#!/usr/bin/python

# Copyright (c) 2010 Google Inc. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import gyp
import gyp.common
import gyp.system_test
import os.path
import subprocess
import sys

generator_default_variables = {
  'EXECUTABLE_PREFIX': '',
  'EXECUTABLE_SUFFIX': '',
  'OS': 'linux',
  'STATIC_LIB_PREFIX': 'lib',
  'SHARED_LIB_PREFIX': 'lib',
  'STATIC_LIB_SUFFIX': '.a',
  'SHARED_LIB_SUFFIX': '.so',
  'INTERMEDIATE_DIR': '$b/geni',
  'SHARED_INTERMEDIATE_DIR': '$b/gen',
  'PRODUCT_DIR': '$b/',
  'SHARED_LIB_DIR': '$b/lib',
  'LIB_DIR': '$b/',

  # Special variables that may be used by gyp 'rule' targets.
  # We generate definitions for these variables on the fly when processing a
  # rule.
  'RULE_INPUT_ROOT': '$root',
  'RULE_INPUT_PATH': '$source',
  'RULE_INPUT_EXT': '$ext',
  'RULE_INPUT_NAME': '$name',
}

NINJA_BASE = """\
builddir = %(builddir_name)s
# Short alias for builddir.
b = %(builddir_name)s

cc = %(cc)s
cxx = %(cxx)s

rule cc
  depfile = $out.d
  description = CC $out
  command = $cc -MMD -MF $out.d $defines $includes $cflags $cflags_cc \\
    -c $in -o $out

rule cxx
  depfile = $out.d
  description = CXX $out
  command = $cxx -MMD -MF $out.d $defines $includes $cflags $cflags_cxx \\
    -c $in -o $out

rule alink
  description = AR $out
  command = rm -f $out && ar rcsT $out $in

rule solink
  description = SOLINK $out
  command = g++ -shared $ldflags -o $out -Wl,-soname=$soname \\
    -Wl,--start-group $in -Wl,--end-group $libs

rule link
  description = LINK $out
  command = g++ $ldflags -o $out -Wl,-rpath=$b/lib \\
    -Wl,--start-group $in -Wl,--end-group $libs

rule stamp
  description = STAMP $out
  command = touch $out

rule copy
  description = COPY $out
  command = ln -f $in $out 2>/dev/null || cp -af $in $out

""" % {
  'builddir_name': os.environ.get('builddir_name', 'ninja'),
  'cwd': os.getcwd(),
  'cc': os.environ.get('CC', 'gcc'),
  'cxx': os.environ.get('CXX', 'g++'),
}

def QuoteShellArgument(arg):
  return "'" + arg.replace("'", "'" + '"\'"' + "'")  + "'"

def MaybeQuoteShellArgument(arg):
  if '"' in arg or ' ' in arg:
    return QuoteShellArgument(arg)
  return arg

class NinjaWriter:
  def __init__(self, target_outputs, base_dir, path):
    self.target_outputs = target_outputs
    self.base_dir = base_dir
    self.path = path
    self.file = open(path, 'w')
    self.variables = {}  # XXX take in global values.

  def InputPath(self, path):
    if path.startswith('$'):
      return path
    return os.path.normpath(os.path.join(self.base_dir, path))

  def OutputPath(self, path):
    if path.startswith('$'):
      return path
    return os.path.normpath(os.path.join('$b/obj', self.name, self.base_dir, path))

  def StampPath(self, name):
    return os.path.join('$b/obj', self.name, name + '.stamp')

  def WriteSpec(self, spec, config):
    self.name = spec['target_name']  # XXX remove bad chars

    if spec['type'] == 'settings':
      return None

    # Compute predepends for all rules.
    prebuild_deps = []
    # self.prebuild_stamp is the filename that all our files depend upon,
    # if any.
    self.prebuild_stamp = None
    if 'dependencies' in spec:
      prebuild_deps = [x
                       for x, _ in [self.target_outputs.get(dep, (None, False))
                                    for dep in spec['dependencies']]
                       if x]
    if prebuild_deps:
      self.prebuild_stamp = self.StampPath('predepends')
      self.WriteEdge([self.prebuild_stamp], 'stamp', prebuild_deps,
                     use_prebuild_stamp=False)
      self.WriteLn()

    sources_predepends = []
    extra_sources = []
    if 'actions' in spec:
      sources_predepends.append(
        self.WriteActions(spec['actions'], extra_sources))

    if 'rules' in spec:
      sources_predepends.append(
        self.WriteRules(spec['rules'], extra_sources))

    if 'copies' in spec:
      sources_predepends.append(
        self.WriteCopies(spec['copies']))

    link_deps = []
    sources = spec.get('sources', []) + extra_sources
    if sources:
      link_deps = self.WriteSources(config, sources, sources_predepends)
      # Some actions/rules output 'sources' that are already object files.
      link_deps += [f for f in sources if f.endswith('.o')]

    # The final output of our target depends on the last output of the
    # above steps.
    final_deps = link_deps or sources_predepends
    if self.prebuild_stamp and not final_deps:
      final_deps = [self.prebuild_stamp]
    if not final_deps:
      print 'warning:', self.name, 'missing output dependencies'
    return self.WriteTarget(spec, config, final_deps)

  def WriteActions(self, actions, extra_sources):
    all_outputs = []
    for action in actions:
      # First write out a rule for the action.
      # XXX we shouldn't need to qualify names; we do it because currently
      # the rule namespace is global, but it really should be scoped to the
      # subninja.
      name = self.name + '.' + action['action_name'].replace(' ', '_')
      args = action['action']
      command = ''
      if self.base_dir:
        # The command expects to be run from the current directory.
        # cd into the directory before running, and adjust all the
        # paths to point to the proper locations.
        command = 'cd %s; ' % self.base_dir
        cdup = '../' * len(self.base_dir.split('/'))
        args = [arg.replace('$b', cdup + '$b') for arg in args]

      command += gyp.common.EncodePOSIXShellList(args)

      if 'message' in action:
        description = 'ACTION ' + action['message']
      else:
        description = 'ACTION %s: %s' % (self.name, action['action_name'])
      self.WriteRule(name=name, command=command, description=description)

      inputs = [self.InputPath(i) for i in action['inputs']]
      if int(action.get('process_outputs_as_sources', False)):
        extra_sources += action['outputs']
      # Though it looks like a typo, we really do intentionally use
      # the input path for outputs.  This is because gyp tests assume
      # one action can output a file and another can then read it; in
      # the Chrome gyp files, outputs like these are always explicitly
      # scoped to one of the intermediate generated files directories,
      # so the InputPath() call is a no-op.
      outputs = [self.InputPath(o) for o in action['outputs']]

      # Then write out an edge using the rule.
      self.WriteEdge(outputs, name, inputs)
      all_outputs += outputs

      self.WriteLn()

    # Write out a stamp file for all the actions.
    stamp = self.StampPath('actions')
    self.WriteEdge([stamp], 'stamp', all_outputs)
    return stamp

  def WriteRules(self, rules, extra_sources):
    all_outputs = []
    for rule in rules:
      # First write out a rule for the rule action.
      # XXX we shouldn't need to qualify names; we do it because currently
      # the rule namespace is global, but it really should be scoped to the
      # subninja.
      self.WriteLn('# rule: ' + repr(rule))
      name = self.name + '.' + rule['rule_name'].replace(' ', '_')
      args = rule['action']
      command = ''
      if self.base_dir:
        # The command expects to be run from the current directory.
        # cd into the directory before running, and adjust all the
        # paths to point to the proper locations.
        command = 'cd %s; ' % self.base_dir
        cdup = '../' * len(self.base_dir.split('/'))
        args = args[:]
        for i, arg in enumerate(args):
          args[i] = args[i].replace('$b', cdup + '$b')
          args[i] = args[i].replace('$source', cdup + '$source')

      command += gyp.common.EncodePOSIXShellList(args)

      if 'message' in rule:
        description = 'RULE ' + rule['message']
      else:
        description = 'RULE %s: %s $source' % (self.name, rule['rule_name'])
      self.WriteRule(name=name, command=command, description=description)
      self.WriteLn()

      # TODO: if the command references the outputs directly, we should
      # simplify it to just use $out.

      # Compute which edge-scoped variables all build rules will need
      # to provide.
      special_locals = ('source', 'root', 'ext', 'name')
      needed_variables = set(['source'])
      for argument in args:
        for var in special_locals:
          if '$' + var in argument:
            needed_variables.add(var)

      # For each source file, write an edge that generates all the outputs.
      for source in rule.get('rule_sources', []):
        basename = os.path.basename(source)
        root, ext = os.path.splitext(basename)
        source = self.InputPath(source)

        outputs = []
        for output in rule['outputs']:
          outputs.append(output.replace('$root', root))

        extra_bindings = []
        for var in needed_variables:
          if var == 'root':
            extra_bindings.append(('root', root))
          elif var == 'source':
            extra_bindings.append(('source', source))
          elif var == 'ext':
            extra_bindings.append(('ext', ext))
          elif var == 'name':
            extra_bindings.append(('name', basename))
          else:
            assert var == None, repr(var)

        inputs = map(self.InputPath, rule.get('inputs', []))
        # XXX need to add extra dependencies on rule inputs
        # (e.g. if generator program changes, we need to rerun)
        self.WriteEdge(outputs, name, [source],
                       implicit_inputs=inputs,
                       extra_bindings=extra_bindings)

        if int(rule.get('process_outputs_as_sources', False)):
          extra_sources += outputs

        all_outputs.extend(outputs)

    # Write out a stamp file for all the actions.
    stamp = self.StampPath('rules')
    self.WriteEdge([stamp], 'stamp', all_outputs)
    self.WriteLn()
    return stamp

  def WriteCopies(self, copies):
    outputs = []
    for copy in copies:
      for path in copy['files']:
        # Normalize the path so trailing slashes don't confuse us.
        path = os.path.normpath(path)
        filename = os.path.split(path)[1]
        src = self.InputPath(path)
        # See discussion of InputPath in WriteActions for why we use it here.
        dst = self.InputPath(os.path.join(copy['destination'], filename))
        self.WriteEdge([dst], 'copy', [src])
        outputs.append(dst)

    stamp = self.StampPath('copies')
    self.WriteEdge([stamp], 'stamp', outputs)
    self.WriteLn()
    return stamp

  def WriteSources(self, config, sources, predepends):
    self.WriteVariableList('defines', ['-D' + d for d in config.get('defines', [])],
                           quoter=MaybeQuoteShellArgument)
    includes = [self.InputPath(i) for i in config.get('include_dirs', [])]
    self.WriteVariableList('includes', ['-I' + i for i in includes])
    self.WriteVariableList('cflags', config.get('cflags'))
    self.WriteVariableList('cflags_cc', config.get('cflags_c'))
    self.WriteVariableList('cflags_cxx', config.get('cflags_cc'))
    self.WriteLn()
    outputs = []
    for source in sources:
      filename, ext = os.path.splitext(source)
      ext = ext[1:]
      if ext in ('cc', 'cpp', 'cxx'):
        command = 'cxx'
      elif ext in ('c', 's', 'S'):
        command = 'cc'
      else:
        # if ext in ('h', 'hxx'):
        # elif ext in ('re', 'gperf', 'grd', ):
        continue
      input = self.InputPath(source)
      output = self.OutputPath(filename + '.o')
      self.WriteEdge([output], command, [input],
                     order_only_inputs=predepends)
      outputs.append(output)
    self.WriteLn()
    return outputs

  def WriteTarget(self, spec, config, final_deps):
    # XXX only write these for rules that will use them
    self.WriteVariableList('ldflags', config.get('ldflags'))
    self.WriteVariableList('libs', spec.get('libraries'))

    output = self.ComputeOutput(spec)

    if 'dependencies' in spec:
      extra_deps = set()
      for dep in spec['dependencies']:
        input, linkable = self.target_outputs.get(dep, (None, False))
        if input and linkable:
          extra_deps.add(input)
      final_deps.extend(list(extra_deps))
    command_map = {
      'executable':      'link',
      'static_library':  'alink',
      'loadable_module': 'solink',
      'shared_library':  'solink',
      'none':            'stamp',
    }
    command = command_map[spec['type']]
    extra_bindings = []
    if command == 'solink':
      extra_bindings.append(('soname', os.path.split(output)[1]))
    self.WriteEdge([output], command, final_deps,
                   extra_bindings=extra_bindings,
                   use_prebuild_stamp=False)

    # Write a short name to build this target.  This benefits both the
    # "build chrome" case as well as the gyp tests, which expect to be
    # able to run actions and build libraries by their short name.
    self.WriteEdge([self.name], 'phony', [output],
                   use_prebuild_stamp=False)

    return output

  def ComputeOutputFileName(self, spec):
    target = spec['target_name']

    # Snip out an extra 'lib' if appropriate.
    if '_library' in spec['type'] and target[:3] == 'lib':
      target = target[3:]

    if spec['type'] in ('static_library', 'loadable_module', 'shared_library'):
      prefix = spec.get('product_prefix', 'lib')

    if spec['type'] == 'static_library':
      return '%s%s.a' % (prefix, target)
    elif spec['type'] in ('loadable_module', 'shared_library'):
      return '%s%s.so' % (prefix, target)
    elif spec['type'] == 'none':
      return '%s.stamp' % target
    elif spec['type'] == 'settings':
      return None
    elif spec['type'] == 'executable':
      return spec.get('product_name', target)
    else:
      raise 'Unhandled output type', spec['type']

  def ComputeOutput(self, spec):
    filename = self.ComputeOutputFileName(spec)

    if 'product_name' in spec:
      print 'XXX ignoring product_name', spec['product_name']
    assert 'product_extension' not in spec

    if 'product_dir' in spec:
      path = os.path.join(spec['product_dir'], filename)
      print 'pdir', path
      return path

    # Executables and loadable modules go into the output root,
    # libraries go into shared library dir, and everything else
    # goes into the normal place.
    if spec['type'] in ('executable', 'loadable_module'):
      return os.path.join('$b/', filename)
    elif spec['type'] == 'shared_library':
      return os.path.join('$b/lib', filename)
    else:
      return self.OutputPath(filename)

  def WriteRule(self, name, command, description=None):
    self.WriteLn('rule %s' % name)
    self.WriteLn('  command = %s' % command)
    if description:
      self.WriteLn('  description = %s' % description)

  def WriteEdge(self, outputs, command, inputs,
                implicit_inputs=[],
                order_only_inputs=[],
                use_prebuild_stamp=True,
                extra_bindings=[]):
    extra_inputs = order_only_inputs[:]
    if use_prebuild_stamp and self.prebuild_stamp:
      extra_inputs.append(self.prebuild_stamp)
    if implicit_inputs:
      implicit_inputs = ['|'] + implicit_inputs
    if extra_inputs:
      extra_inputs = ['||'] + extra_inputs
    self.WriteList('build ' + ' '.join(outputs) + ': ' + command,
                   inputs + implicit_inputs + extra_inputs)
    if extra_bindings:
      for key, val in extra_bindings:
        self.WriteLn('  %s = %s' % (key, val))

  def WriteVariableList(self, var, values, quoter=lambda x: x):
    if self.variables.get(var, []) == values:
      return
    self.variables[var] = values
    self.WriteList(var + ' =', values, quoter=quoter)

  def WriteList(self, decl, values, quoter=lambda x: x):
    self.Write(decl)
    if not values:
      self.WriteLn()
      return

    col = len(decl) + 3
    for value in values:
      value = quoter(value)
      if col != 0 and col + len(value) >= 78:
        self.WriteLn(' \\')
        self.Write(' ' * 4)
        col = 4
      else:
        self.Write(' ')
        col += 1
      self.Write(value)
      col += len(value)
    self.WriteLn()

  def Write(self, *args):
    self.file.write(' '.join(args))

  def WriteLn(self, *args):
    self.file.write(' '.join(args) + '\n')


def CalculateVariables(default_variables, params):
  """Calculate additional variables for use in the build (called by gyp)."""
  cc_target = os.environ.get('CC.target', os.environ.get('CC', 'cc'))
  default_variables['LINKER_SUPPORTS_ICF'] = \
      gyp.system_test.TestLinkerSupportsICF(cc_command=cc_target)


def tput(str):
  return subprocess.Popen(['tput',str], stdout=subprocess.PIPE).communicate()[0]
tput_clear = tput('el1')
import time
def OverPrint(*args):
  #sys.stdout.write(tput_clear + '\r' + ' '.join(args))
  sys.stdout.write(' '.join(args) + '\n')
  sys.stdout.flush()
  #time.sleep(0.01)  # XXX

def GenerateOutput(target_list, target_dicts, data, params):
  options = params['options']
  generator_flags = params.get('generator_flags', {})
  builddir_name = generator_flags.get('output_dir', 'ninja')

  src_root = options.depth
  master_ninja = open(os.path.join(src_root, 'build.ninja'), 'w')
  master_ninja.write(NINJA_BASE)

  all_targets = set()
  for build_file in params['build_files']:
    for target in gyp.common.AllTargets(target_list, target_dicts, build_file):
      all_targets.add(target)
  all_outputs = set()

  subninjas = set()
  target_outputs = {}
  for qualified_target in target_list:
    # qualified_target is like: third_party/icu/icu.gyp:icui18n#target
    #OverPrint(qualified_target)
    build_file, target, _ = gyp.common.ParseQualifiedTarget(qualified_target)

    build_file = gyp.common.RelativePath(build_file, src_root)
    base_path = os.path.dirname(build_file)
    ninja_path = os.path.join(base_path, target + '.ninja')
    output_file = os.path.join(src_root, ninja_path)
    spec = target_dicts[qualified_target]
    if 'config' in generator_flags:
      config_name = generator_flags['config']
    else:
      config_name = 'Default'
      if config_name not in spec['configurations']:
        config_name = spec['default_configuration']
    config = spec['configurations'][config_name]

    writer = NinjaWriter(target_outputs, base_path, output_file)
    subninjas.add(ninja_path)

    output = writer.WriteSpec(spec, config)
    if output:
      linkable = spec['type'] in ('static_library', 'shared_library')
      target_outputs[qualified_target] = (output, linkable)

      if qualified_target in all_targets:
        all_outputs.add(output)

  for ninja in subninjas:
    print >>master_ninja, 'subninja', ninja

  if all_outputs:
    print >>master_ninja, 'build all: phony ||' + ' '.join(all_outputs)

  master_ninja.close()
  OverPrint('done.\n')
