
"""
Here are all subprocess, threading etc related utilities,
most of them quite low level.
"""

from threading import Lock, currentThread
import sys
import os
from StringIO import StringIO
from contextlib import contextmanager
import pickle
import types
import marshal
from importlib import import_module
import errno
import time
import numpy


def execInMainProc(func):
  global isMainProcess
  if isMainProcess:
    return func()
  else:
    assert _AsyncCallQueue.Self, "works only if called via asyncCall"
    return _AsyncCallQueue.Self.asyncExecClient(func)


def ExecInMainProcDecorator(func):
  def decoratedFunc(*args, **kwargs):
    return execInMainProc(lambda: func(*args, **kwargs))
  return decoratedFunc


class AsyncInterrupt(BaseException):
  pass


class ForwardedKeyboardInterrupt(Exception):
  pass


class _AsyncCallQueue:
  Self = None

  class Types:
    result = 0
    exception = 1
    asyncExec = 2

  def __init__(self, queue):
    assert not self.Self
    self.__class__.Self = self
    self.mutex = Lock()
    self.queue = queue

  def put(self, type, value):
    self.queue.put((type, value))

  def asyncExecClient(self, func):
    with self.mutex:
      self.put(self.Types.asyncExec, func)
      t, value = self.queue.get()
      if t == self.Types.result:
        return value
      elif t == self.Types.exception:
        raise value
      else:
        assert False, "bad behavior of asyncCall in asyncExec (%r)" % t

  @classmethod
  def asyncExecHost(clazz, task, func):
    q = task
    name = "<unknown>"
    try:
      name = repr(func)
      res = func()
    except Exception as exc:
      print "Exception in asyncExecHost", name, exc
      q.put((clazz.Types.exception, exc))
    else:
      try:
        q.put((clazz.Types.result, res))
      except IOError:
        # broken pipe or so. parent quit. treat like a SIGINT
        raise KeyboardInterrupt


def asyncCall(func, name=None, mustExec=False):
  """
  This executes func() in another process and waits/blocks until
  it is finished. The returned value is passed back to this process
  and returned. Exceptions are passed back as well and will be
  reraised here.

  If `mustExec` is set, the other process must `exec()` after the `fork()`.
  If it is not set, it might omit the `exec()`, depending on the platform.
  """

  def doCall(queue):
    q = _AsyncCallQueue(queue)
    try:
      try:
        res = func()
      except KeyboardInterrupt as exc:
        print "Exception in asyncCall", name, ": KeyboardInterrupt"
        q.put(q.Types.exception, ForwardedKeyboardInterrupt(exc))
      except BaseException as exc:
        print "Exception in asyncCall", name
        sys.excepthook(*sys.exc_info())
        q.put(q.Types.exception, exc)
      else:
        q.put(q.Types.result, res)
    except (KeyboardInterrupt, ForwardedKeyboardInterrupt):
      print "asyncCall: SIGINT in put, probably the parent died"
    # ignore

  task = AsyncTask(func=doCall, name=name, mustExec=mustExec)

  while True:
    # If there is an unhandled exception in doCall or the process got killed/segfaulted or so,
    # this will raise an EOFError here.
    # However, normally, we should catch all exceptions and just reraise them here.
    t,value = task.get()
    if t == _AsyncCallQueue.Types.result:
      return value
    elif t == _AsyncCallQueue.Types.exception:
      raise value
    elif t == _AsyncCallQueue.Types.asyncExec:
      _AsyncCallQueue.asyncExecHost(task, value)
    else:
      assert False, "unknown _AsyncCallQueue type %r" % t



def attrChain(base, *attribs, **kwargs):
  default = kwargs.get("default", None)
  obj = base
  for attr in attribs:
    if obj is None: return default
    obj = getattr(obj, attr, None)
  if obj is None: return default
  return obj


# This is needed in some cases to avoid pickling problems with bounded funcs.
def funcCall(attrChainArgs, args=()):
  f = attrChain(*attrChainArgs)
  return f(*args)


Unpickler = pickle.Unpickler
CellType = type((lambda x: lambda: x)(0).func_closure[0])
ModuleType = type(sys)

def makeFuncCell(value):
  return (lambda: value).func_closure[0]

def getModuleDict(modname):
  mod = import_module(modname)
  return mod.__dict__

def getModNameForModDict(obj):
  """
  :type obj: dict
  :rtype: str | None
  :returns The module name or None. It will not return '__main__' in any case
  because that likely will not be the same in the unpickling environment.
  """
  mods = {id(mod.__dict__): modname for (modname, mod) in sys.modules.items() if mod and modname != "__main__"}
  modname = mods.get(id(obj), None)
  return modname

def getNormalDict(d):
  """
  :type d: dict[str] | dictproxy
  :rtype: dict[str]
  It also removes getset_descriptor. New-style classes have those.
  """
  r = {}
  for k, v in d.items():
    if isinstance(v, types.GetSetDescriptorType): continue
    r[k] = v
  return r

def make_numpy_ndarray_fromstring(s, dtype, shape):
  return numpy.fromstring(s, dtype=dtype).reshape(shape)


class Pickler(pickle.Pickler):
  """
  We extend the standard Pickler to be able to pickle some more types,
  such as lambdas and functions, code, func cells, buffer and more.
  """

  def __init__(self, *args, **kwargs):
    if not "protocol" in kwargs:
      kwargs["protocol"] = pickle.HIGHEST_PROTOCOL
    pickle.Pickler.__init__(self, *args, **kwargs)
  dispatch = pickle.Pickler.dispatch.copy()

  def save_func(self, obj):
    try:
      self.save_global(obj)
      return
    except pickle.PicklingError:
      pass
    assert type(obj) is types.FunctionType
    self.save(types.FunctionType)
    self.save((
      obj.func_code,
      obj.func_globals,
      obj.func_name,
      obj.func_defaults,
      obj.func_closure,
    ))
    self.write(pickle.REDUCE)
    if id(obj) not in self.memo:  # Could be if we recursively landed here. See also pickle.save_tuple().
      self.memoize(obj)
  dispatch[types.FunctionType] = save_func

  def save_method(self, obj):
    try:
      self.save_global(obj)
      return
    except pickle.PicklingError:
      pass
    assert type(obj) is types.MethodType
    self.save(types.MethodType)
    self.save((obj.im_func, obj.im_self, obj.im_class))
    self.write(pickle.REDUCE)
    self.memoize(obj)
  dispatch[types.MethodType] = save_method

  def save_code(self, obj):
    assert type(obj) is types.CodeType
    self.save(marshal.loads)
    self.save((marshal.dumps(obj),))
    self.write(pickle.REDUCE)
    self.memoize(obj)
  dispatch[types.CodeType] = save_code

  def save_cell(self, obj):
    assert type(obj) is CellType
    self.save(makeFuncCell)
    self.save((obj.cell_contents,))
    self.write(pickle.REDUCE)
    self.memoize(obj)
  dispatch[CellType] = save_cell

  # We also search for module dicts and reference them.
  # This is for FunctionType.func_globals.
  def intellisave_dict(self, obj):
    modname = getModNameForModDict(obj)
    if modname:
      self.save(getModuleDict)
      self.save((modname,))
      self.write(pickle.REDUCE)
      self.memoize(obj)
      return
    self.save_dict(obj)
  dispatch[types.DictionaryType] = intellisave_dict

  def save_module(self, obj):
    modname = getModNameForModDict(obj.__dict__)
    if modname:
      self.save(import_module)
      self.save((modname,))
      self.write(pickle.REDUCE)
      self.memoize(obj)
      return
    # We could maybe construct it manually. For now, just fail.
    raise pickle.PicklingError('cannot pickle module %r' % obj)
  dispatch[ModuleType] = save_module

  def save_buffer(self, obj):
    self.save(buffer)
    self.save((str(obj),))
    self.write(pickle.REDUCE)
  dispatch[types.BufferType] = save_buffer

  def save_ndarray(self, obj):
    # For some reason, Numpy fromstring/tostring is faster than Numpy loads/dumps.
    self.save(make_numpy_ndarray_fromstring)
    self.save((obj.tostring(), str(obj.dtype), obj.shape))
    self.write(pickle.REDUCE)
  dispatch[numpy.ndarray] = save_ndarray

  # Overwrite to avoid the broken pickle.whichmodule() which might return "__main__".
  def save_global(self, obj, name=None, pack=pickle.struct.pack):
    assert obj
    assert id(obj) not in self.memo
    if name is None:
      name = obj.__name__

    module = getattr(obj, "__module__", None)
    if module is None or module == "__main__":
      module = pickle.whichmodule(obj, name)
    if module is None or module == "__main__":
      raise pickle.PicklingError(
          "Can't pickle %r: module not found: %s" % (obj, module))

    try:
      __import__(module)
      mod = sys.modules[module]
      klass = getattr(mod, name)
    except (ImportError, KeyError, AttributeError):
      raise pickle.PicklingError(
          "Can't pickle %r: it's not found as %s.%s" % (obj, module, name))
    else:
      if klass is not obj:
        raise pickle.PicklingError(
            "Can't pickle %r: it's not the same object as %s.%s" % (obj, module, name))

    assert "\n" not in module
    assert "\n" not in name
    self.write(pickle.GLOBAL + module + '\n' + name + '\n')
    self.memoize(obj)

  def save_type(self, obj):
    try:
      self.save_global(obj)
      return
    except pickle.PicklingError:
      pass
    # Some types in the types modules are not correctly referenced,
    # such as types.FunctionType. This is fixed here.
    for modname in ["types"]:
      moddict = sys.modules[modname].__dict__
      for modobjname,modobj in moddict.iteritems():
        if modobj is obj:
          self.write(pickle.GLOBAL + modname + '\n' + modobjname + '\n')
          self.memoize(obj)
          return
    # Generic serialization of new-style classes.
    self.save(type)
    self.save((obj.__name__, obj.__bases__, getNormalDict(obj.__dict__)))
    self.write(pickle.REDUCE)
    self.memoize(obj)
  dispatch[types.TypeType] = save_type

  # This is about old-style classes.
  def save_class(self, cls):
    try:
      # First try with a global reference. This works normally. This is the default original pickle behavior.
      self.save_global(cls)
      return
    except pickle.PicklingError:
      pass
    # It didn't worked. But we can still serialize it.
    # Note that this could potentially confuse the code if the class is reference-able in some other way
    # - then we will end up with two versions of the same class.
    self.save(types.ClassType)
    self.save((cls.__name__, cls.__bases__, cls.__dict__))
    self.write(pickle.REDUCE)
    self.memoize(cls)
    return
  dispatch[types.ClassType] = save_class

  # avoid pickling instances of ourself. this mostly doesn't make sense and leads to trouble.
  # however, also doesn't break. it mostly makes sense to just ignore.
  def __getstate__(self): return None
  def __setstate__(self, state): pass


class ExecingProcess:
  """
  This is a replacement for multiprocessing.Process which always
  uses fork+exec, not just fork.
  This ensures that you have a separate independent process.
  This can avoid many types of bugs, such as:
    http://stackoverflow.com/questions/24509650
    http://bugs.python.org/issue6721
    http://stackoverflow.com/questions/8110920
    http://stackoverflow.com/questions/23963997
    https://github.com/numpy/numpy/issues/654
    http://comments.gmane.org/gmane.comp.python.numeric.general/60204
  """

  def __init__(self, target, args, name, env_update):
    self.target = target
    self.args = args
    self.name = name
    self.env_update = env_update
    self.daemon = True
    self.pid = None
    self.exit_status = None

  def start(self):
    assert self.pid is None
    assert self.exit_status is None
    def pipeOpen():
      readend, writeend = os.pipe()
      readend = os.fdopen(readend, "r")
      writeend = os.fdopen(writeend, "w")
      return readend, writeend
    self.pipe_c2p = pipeOpen()
    self.pipe_p2c = pipeOpen()
    self.parent_pid = os.getpid()
    pid = os.fork()
    flags = {key: value for (key, value) in [s.split("=", 1) for s in os.environ.get("THEANO_FLAGS", "").split(",") if s]}
    if 'base_compiledir' in flags:
      offset = flags['base_compiledir'].find("_-_", 1)
      if offset > 1:
        flags['base_compiledir'] = flags['base_compiledir'][:offset]
      flags['base_compiledir'] += '_-_' + self.name.replace(' ','_')
    else:
      flags['base_compiledir'] = '/tmp/theano/' + self.name.replace(' ','_')
    os.environ["THEANO_FLAGS"] = ",".join(["=".join(x) for x in flags.items()])
    if pid == 0: # child
      try:
        sys.stdin.close()  # Force no tty stdin.
        self.pipe_c2p[0].close()
        self.pipe_p2c[1].close()
        py_mod_file = os.path.splitext(__file__)[0] + ".py"
        assert os.path.exists(py_mod_file)
        args = [sys.executable,
                py_mod_file,
                "--forkExecProc",
                str(self.pipe_c2p[1].fileno()),
                str(self.pipe_p2c[0].fileno())]
        if self.env_update:
          os.environ.update(self.env_update)
        os.execv(args[0], args)  # Does not return if successful.
      except BaseException:
        print "ExecingProcess: Error at initialization."
        sys.excepthook(*sys.exc_info())
        sys.exit(1)
      finally:
        sys.exit()
    else: # parent
      self.pipe_c2p[1].close()
      self.pipe_p2c[0].close()
      self.pid = pid
      self.pickler = Pickler(self.pipe_p2c[1])
      self.pickler.dump(self.name)
      self.pickler.dump(self.target)
      self.pickler.dump(self.args)
      self.pipe_p2c[1].flush()

  def _wait(self, options=0):
    assert self.parent_pid == os.getpid()
    assert self.pid
    assert self.exit_status is None
    pid, exit_status = os.waitpid(self.pid, options)
    if pid != self.pid:
      assert pid == 0
      # It's still alive, otherwise we would have get the same pid.
      return
    self.exit_status = exit_status
    self.pid = None

  def is_alive(self):
    if self.pid is None:
      return False
    self._wait(os.WNOHANG)
    return self.pid is not None

  def join(self, timeout=None):
    if not self.is_alive():
      return
    if timeout:
      # Simple and stupid implementation.
      while self.is_alive():
        if timeout < 0:
          break
        if timeout < 1.0:
          time.sleep(timeout)
          break
        else:
          time.sleep(1)
          timeout -= 1
      return
    self._wait()

  Verbose = False

  @staticmethod
  def checkExec():
    if "--forkExecProc" in sys.argv:
      mod_path = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))
      if not any(mod_path == os.path.realpath(s) for s in sys.path):
        sys.path.append(mod_path)
      try:
        import better_exchook
      except ImportError:
        pass  # Doesn't matter.
      else:
        better_exchook.install()
      argidx = sys.argv.index("--forkExecProc")
      writeFileNo = int(sys.argv[argidx + 1])
      readFileNo = int(sys.argv[argidx + 2])
      readend = os.fdopen(readFileNo, "r")
      writeend = os.fdopen(writeFileNo, "w")
      unpickler = Unpickler(readend)
      name = unpickler.load()
      if ExecingProcess.Verbose: print "ExecingProcess child %s (pid %i)" % (name, os.getpid())
      try:
        target = unpickler.load()
        args = unpickler.load()
      except EOFError:
        print "Error: unpickle incomplete"
        raise SystemExit
      ret = target(*args)
      sys.exited = True
      # IOError is probably broken pipe. That probably means that the parent died.
      try: Pickler(writeend).dump(ret)
      except IOError: pass
      try: readend.close()
      except IOError: pass
      try: writeend.close()
      except IOError: pass
      if ExecingProcess.Verbose: print "ExecingProcess child %s (pid %i) finished" % (name, os.getpid())
      raise SystemExit


class ProcConnectionDied(Exception):
  pass


class ExecingProcess_ConnectionWrapper(object):
  """
  Wrapper around _multiprocessing.Connection.
  This is needed to use our own Pickler.
  """

  def __init__(self, fd=None, conn=None):
    self.fd = fd
    if self.fd:
      from _multiprocessing import Connection
      self.conn = Connection(fd)
    elif conn:
      self.conn = conn
    else:
      self.conn = None

  def __getstate__(self):
    if self.fd is not None:
      return {"fd": self.fd}
    elif self.conn is not None:
      return {"conn": self.conn}  # Try to pickle the connection.
    else:
      return {}
  def __setstate__(self, state):
    self.__init__(**state)

  def __getattr__(self, attr): return getattr(self.conn, attr)

  def _check_closed(self):
    if self.conn.closed: raise ProcConnectionDied("connection closed")
  def _check_writable(self):
    if not self.conn.writable: raise ProcConnectionDied("connection not writeable")
  def _check_readable(self):
    if not self.conn.readable: raise ProcConnectionDied("connection not readable")

  def poll(self, *args, **kwargs):
    while True:
      try:
        return self.conn.poll(*args, **kwargs)
      except IOError as e:
        if e.errno == errno.EINTR:
          # http://stackoverflow.com/questions/14136195
          # We can just keep trying.
          continue
        raise ProcConnectionDied("poll IOError: %s" % e)
      except EOFError as e:
        raise ProcConnectionDied("poll EOFError: %s" % e)

  def send_bytes(self, value):
    try:
      self.conn.send_bytes(value)
    except (EOFError, IOError) as e:
      raise ProcConnectionDied("send_bytes EOFError/IOError: %s" % e)

  def send(self, value):
    self._check_closed()
    self._check_writable()
    buf = StringIO()
    Pickler(buf).dump(value)
    self.send_bytes(buf.getvalue())

  def recv_bytes(self):
    while True:
      try:
        return self.conn.recv_bytes()
      except IOError as e:
        if e.errno == errno.EINTR:
          # http://stackoverflow.com/questions/14136195
          # We can just keep trying.
          continue
        raise ProcConnectionDied("recv_bytes IOError: %s" % e)
      except EOFError as e:
        raise ProcConnectionDied("recv_bytes EOFError: %s" % e)

  def recv(self):
    self._check_closed()
    self._check_readable()
    buf = self.recv_bytes()
    f = StringIO(buf)
    res = Unpickler(f).load()
    return res


def ExecingProcess_Pipe():
  """
  This is like multiprocessing.Pipe(duplex=True).
  It uses our own ExecingProcess_ConnectionWrapper.
  """
  import socket
  s1, s2 = socket.socketpair()
  c1 = ExecingProcess_ConnectionWrapper(os.dup(s1.fileno()))
  c2 = ExecingProcess_ConnectionWrapper(os.dup(s2.fileno()))
  s1.close()
  s2.close()
  return c1, c2


def Pipe_ConnectionWrapper(*args, **kwargs):
  from multiprocessing import Pipe
  c1, c2 = Pipe(*args, **kwargs)
  c1 = ExecingProcess_ConnectionWrapper(conn=c1)
  c2 = ExecingProcess_ConnectionWrapper(conn=c2)
  return c1, c2


if sys.platform == "win32":
  from multiprocessing.forking import Popen as mp_Popen

  class Win32_mp_Popen_wrapper:
    def __init__(self, env_update):
      self.env = os.environ.copy()
      self.env.update(env_update)

    class Popen(mp_Popen):
      # noinspection PyMissingConstructor
      def __init__(self, process_obj, env):
        # No super init call by intention!

        from multiprocessing.forking import duplicate, get_command_line, _python_exe, close, get_preparation_data, HIGHEST_PROTOCOL, dump
        import msvcrt
        import _subprocess

        # create pipe for communication with child
        rfd, wfd = os.pipe()

        # get handle for read end of the pipe and make it inheritable
        rhandle = duplicate(msvcrt.get_osfhandle(rfd), inheritable=True)
        os.close(rfd)

        # start process
        cmd = get_command_line() + [rhandle]
        cmd = ' '.join('"%s"' % x for x in cmd)
        hp, ht, pid, tid = _subprocess.CreateProcess(
          _python_exe, cmd, None, None, 1, 0, env, None, None
        )
        ht.Close()
        close(rhandle)

        # set attributes of self
        self.pid = pid
        self.returncode = None
        self._handle = hp

        # send information to child
        prep_data = get_preparation_data(process_obj._name)
        to_child = os.fdopen(wfd, 'wb')
        mp_Popen._tls.process_handle = int(hp)
        try:
          dump(prep_data, to_child, HIGHEST_PROTOCOL)
          dump(process_obj, to_child, HIGHEST_PROTOCOL)
        finally:
          del mp_Popen._tls.process_handle
          to_child.close()

    def __call__(self, process_obj):
      return self.Popen(process_obj, self.env)


isFork = False  # fork() without exec()
isMainProcess = True


class AsyncTask:
  """
  This uses multiprocessing.Process or ExecingProcess to execute some function.
  In addition, it provides a duplex pipe for communication. This is either
  multiprocessing.Pipe or ExecingProcess_Pipe.
  """

  def __init__(self, func, name=None, mustExec=False, env_update=None):
    """
    :param func: a function which gets a single parameter,
      which will be a reference to our instance in the fork,
      so that it can use our communication methods put/get.
    :type str name: name for the sub process
    :param bool mustExec: if True, we do fork+exec, not just fork
    :param dict[str,str] env_update: for mustExec, also update these env vars
    """
    self.name = name or "unnamed"
    self.func = func
    self.mustExec = mustExec
    self.env_update = env_update
    self.parent_pid = os.getpid()
    proc_args = {
      "target": funcCall,
      "args": ((AsyncTask, "_asyncCall"), (self,)),
      "name": self.name + " worker process"
    }
    if mustExec and sys.platform != "win32":
      self.Process = ExecingProcess
      self.Pipe = ExecingProcess_Pipe
      proc_args["env_update"] = env_update
    else:
      from multiprocessing import Process, Pipe
      self.Process = Process
      self.Pipe = Pipe_ConnectionWrapper
    self.parent_conn, self.child_conn = self.Pipe()
    self.proc = self.Process(**proc_args)
    self.proc.daemon = True
    if sys.platform == 'win32':
      self.proc._Popen = Win32_mp_Popen_wrapper(env_update=env_update)
    self.proc.start()
    self.child_conn.close()
    self.child_pid = self.proc.pid
    assert self.child_pid
    self.conn = self.parent_conn

  @staticmethod
  def _asyncCall(self):
    assert self.isChild
    self.parent_conn.close()
    self.conn = self.child_conn # we are the child
    if not self.mustExec and sys.platform != "win32":
      global isFork
      isFork = True
    global isMainProcess
    isMainProcess = False
    try:
      self.func(self)
    except KeyboardInterrupt:
      print "Exception in AsyncTask", self.name, ": KeyboardInterrupt"
      sys.exit(1)
    except SystemExit:
      raise
    except BaseException:
      print "Exception in AsyncTask", self.name
      sys.excepthook(*sys.exc_info())
      sys.exit(1)
    finally:
      self.conn.close()

  def put(self, value):
    self.conn.send(value)

  def get(self):
    thread = currentThread()
    try:
      thread.waitQueue = self
      res = self.conn.recv()
    except EOFError: # this happens when the child died
      raise ForwardedKeyboardInterrupt()
    except Exception:
      raise
    finally:
      thread.waitQueue = None
    return res

  @property
  def isParent(self):
    return self.parent_pid == os.getpid()

  @property
  def isChild(self):
    if self.isParent: return False
    # Note: self.parent_pid != os.getppid() if the parent died.
    return True

  # This might be called from the module code.
  # See OnRequestQueue which implements the same interface.
  def setCancel(self):
    self.conn.close()
    if self.isParent and self.child_pid:
      import signal
      try:
        os.kill(self.child_pid, signal.SIGINT)
      except OSError:
        # Could be that the process already died or so. Just ignore and assume it is dead.
        pass
      self.child_pid = None

  terminate = setCancel  # alias

  def join(self, timeout=None):
    return self.proc.join(timeout=timeout)

  def is_alive(self):
    return self.proc.is_alive()


def WarnMustNotBeInForkDecorator(func):
  class Ctx:
    didWarn = False
  def decoratedFunc(*args, **kwargs):
    global isFork
    if isFork:
      if not Ctx.didWarn:
        print "Must not be in fork!"
        Ctx.didWarn = True
      return None
    return func(*args, **kwargs)
  return decoratedFunc


class ReadWriteLock(object):
  """Classic implementation of ReadWriteLock.
  Note that this partly supports recursive lock usage:
  - Inside a readlock, a writelock will always block!
  - Inside a readlock, another readlock is fine.
  - Inside a writelock, any other writelock or readlock is fine.
  """
  def __init__(self):
    import threading
    self.lock = threading.RLock()
    self.writeReadyCond = threading.Condition(self.lock)
    self.readerCount = 0
  @property
  @contextmanager
  def readlock(self):
    with self.lock:
      self.readerCount += 1
    try: yield
    finally:
      with self.lock:
        self.readerCount -= 1
        if self.readerCount == 0:
          self.writeReadyCond.notifyAll()
  @property
  @contextmanager
  def writelock(self):
    with self.lock:
      while self.readerCount > 0:
        self.writeReadyCond.wait()
      yield


if __name__ == "__main__":
  try:
    ExecingProcess.checkExec()  # Never returns if this proc is called via ExecingProcess.
  except KeyboardInterrupt:
    sys.exit(1)
  print "You are not expected to call this. This is for ExecingProcess."
  sys.exit(1)
