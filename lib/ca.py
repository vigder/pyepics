#!usr/bin/python
#
# low level support for Epics Channel Access
#
#  M Newville <newville@cars.uchicago.edu>
#  The University of Chicago, 2010
#  Epics Open License
"""
EPICS Channel Access Interface

See doc/  for user documentation.

documentation here is developer documentation.
"""
import ctypes
import ctypes.util

import os
import sys
import time
import copy
import atexit

HAS_NUMPY = False
try:
    import numpy
    HAS_NUMPY = True
except ImportError:
    pass

from . import dbr

EPICS_STR_ENCODING = 'ASCII'
PY_VERSION = sys.version_info[0]
def get_strconvertors():
    """create string wrappers to pass to C functions for both
    Python2 and Python3.  Note that the EPICS CA library uses
    char* to represent strings.  In Python3, char* maps to a
    sequence of bytes which must be explicitly converted to a
    Python string by specifying the encoding.  That is, ASCII
    encoding is not implicitly assumed.

    That is, for Python3 one sends and receives sequences of
    bytes to libca. This function returns the translators
    (STR2BYTES, BYTES2STR), assuming the encoding defined in
    EPICS_STR_ENCODING (which is 'ASCII' by default).  
    """
    if PY_VERSION >= 3:
        def s2b(st1):
            'string to byte'
            if isinstance(st1, bytes):
                return st1
            return bytes(st1, EPICS_STR_ENCODING)
        def b2s(st1):
            'byte to string'
            if isinstance(st1, str):
                return st1
            return str(st1, EPICS_STR_ENCODING)
        return s2b, b2s
    return str, str

STR2BYTES, BYTES2STR = get_strconvertors()

def strjoin(sep, seq):
    "join string sequence with a separator"
    if PY_VERSION < 3:
        return sep.join(seq)

    if isinstance(sep, bytes):
        sep = BYTES2STR(sep)
    if isinstance(seq[0], bytes): 
        seq = [BYTES2STR(i) for i in seq]
    return sep.join(seq)
    
## print to stdout
def write(msg, newline=True, flush=True):
    """write message to stdout"""
    sys.stdout.write(msg)
    if newline:
        sys.stdout.write("\n")
    if flush:
        sys.stdout.flush()
    
## holder for shared library
libca = None

## PREEMPTIVE_CALLBACK determines the CA context
PREEMPTIVE_CALLBACK = True
# PREEMPTIVE_CALLBACK = False

AUTO_CLEANUP = True

##
# maximum element count for auto-monitoring of PVs in epics.pv
# and for automatic conversion of numerical array data to numpy arrays
AUTOMONITOR_MAXLENGTH = 16384

## default timeout for connection
#   This should be kept fairly short --
#   as connection will be tried repeatedly
DEFAULT_CONNECTION_TIMEOUT = 2.0

## Cache of existing channel IDs:
#  pvname: {'chid':chid, 'conn': isConnected,
#           'ts': ts_conn, 'callbacks': [ user_callback... ])
#  isConnected   = True/False: if connected.
#  ts_conn       = ts of last connection event or failed attempt.
#  user_callback = one or more user functions to be called on change (accumulated in the cache)
_cache  = {}

## Cache of pvs waiting for put to be done.
_put_done =  {}
        
class ChannelAccessException(Exception):
    """Channel Access Exception: General Errors"""
    def __init__(self, fcn, msg):
        Exception.__init__(self)
        self.fcn = fcn
        self.msg = msg
    def __str__(self):
        return " %s returned '%s'" % (self.fcn, self.msg)

class CASeverityException(Exception):
    """Channel Access Severity Check Exception:
    PySEVCHK got unexpected return value"""
    def __init__(self, fcn, msg):
        Exception.__init__(self)
        self.fcn = fcn
        self.msg = msg
    def __str__(self):
        return " %s returned '%s'" % (self.fcn, self.msg)

def find_libca():
    """
    find location of ca dynamic library
    """
    search_path = [os.path.split( os.path.abspath(__file__))[0]]
    search_path.extend(sys.path)
    path_sep = ':'
    # For windows, we assume the DLLs are installed with the library
    if os.name == 'nt':
        path_sep = ';'
        search_path.append(os.path.join(sys.prefix, 'DLLs'))
    
    search_path.extend(os.environ['PATH'].split(path_sep))

    os.environ['PATH'] = path_sep.join(search_path)  

    # first, try the ctypes utility, which *should* work
    # with LD_LIBRARY_PATH or ldconfig 
    dllpath  = ctypes.util.find_library('ca')
    if dllpath is not None:
        return dllpath

    ## OK, simplest version didn't work, look explicity through path
    known_hosts = {'Linux':   ('linux-x86', 'linux-x86_64') ,
                   'Darwin':  ('darwin-ppc', 'darwin-x86'),
                   'SunOS':   ('solaris-sparc', 'solaris-sparc-gnu') }

    
    if os.name == 'posix':
        libname = 'libca.so'
        ldpath = os.environ.get('LD_LIBRARY_PATH', '').split(':')

        if sys.platform == 'darwin':
            ldpath = os.environ.get('DYLD_LIBRARY_PATH', '').split(':')
            libname = 'libca.dylib'

        epics_base = os.environ.get('EPICS_BASE', '.')
        host_arch = os.uname()[0]
        if host_arch in known_hosts:
            epicspath = []
            for adir in known_hosts[host_arch]:
                epicspath.append(os.path.join(epics_base, 'lib', adir))
        for adir in search_path + ldpath + epicspath + sys.path:
            if os.path.exists(adir) and os.path.isdir(adir):
                if libname in os.listdir(adir):
                    return os.path.join(adir, libname)

    raise ChannelAccessException('find_libca',
                                 'Cannot find Epics CA DLL')

def initialize_libca():
    """ load DLL (shared object library) to establish Channel Access
    Connection. The value of PREEMPTIVE_CALLBACK sets the pre-emptive
    callback model: 
        False  no preemptive callbacks. pend_io/pend_event must be used.
        True   preemptive callbaks will be done.
    Returns libca where 
        libca = ca library object, used for all subsequent ca calls

 Note that this function must be called prior to any real ca calls.
    """
    if 'EPICS_CA_MAX_ARRAY_BYTES' not in os.environ:
        os.environ['EPICS_CA_MAX_ARRAY_BYTES'] = "%i" %  2**24
        
    dllname = find_libca()
    load_dll = ctypes.cdll.LoadLibrary
    global libca
    if os.name == 'nt':
        load_dll = ctypes.windll.LoadLibrary
    try:
        libca = load_dll(dllname)
    except:
        raise ChannelAccessException('initialize_libca',
                                     'Loading Epics CA DLL failed')
        
    ca_context = {False:0, True:1}[PREEMPTIVE_CALLBACK]
    ret = libca.ca_context_create(ca_context)
    if ret != dbr.ECA_NORMAL:
        raise ChannelAccessException('initialize_libca',
                                     'Cannot create Epics CA Context')

    # set argtypes and non-default return types
    # for several libca functions here
    libca.ca_pend_event.argtypes  = [ctypes.c_double]
    libca.ca_pend_io.argtypes     = [ctypes.c_double]
    libca.ca_client_status.argtypes = [ctypes.c_void_p, ctypes.c_long]
    libca.ca_sg_block.argtypes    = [ctypes.c_ulong, ctypes.c_double]

    libca.ca_current_context.restype = ctypes.c_void_p
    libca.ca_version.restype   = ctypes.c_char_p
    libca.ca_host_name.restype = ctypes.c_char_p
    libca.ca_name.restype      = ctypes.c_char_p
    libca.ca_message.restype   = ctypes.c_char_p

    # save value offests used for unpacking
    # TIME and CTRL data as an array in dbr module
    dbr.value_offset = (39*ctypes.c_short).in_dll(libca,'dbr_value_offset')

    if AUTO_CLEANUP:
        atexit.register(finalize_libca)
    return libca

def finalize_libca(maxtime=10.0):
    """shutdown channel access:
    run clear_channel(chid) for all chids in _cache
    then flush_io() and poll() a few times.
    """
    global libca
    global _cache
    if libca is None:
        return
    try:
        start_time = time.time()
        flush_io()
        poll()
        for ctx in _cache.values():
            for key in list(ctx.keys()):
                ctx.pop(key)
        _cache.clear()
        flush_count = 0
        while (flush_count < 5 and
               time.time()-start_time < maxtime):
            flush_io()
            poll()
            flush_count += 1
        context_destroy()
        libca = None
    except StandardError:
        pass

def show_cache(print_out=True):
    """Show list of cached PVs"""
    out = []
    out.append('#  PV name    Is Connected?   Channel ID  Context')
    out.append('#---------------------------------------')
    global _cache
    for context, context_chids in  list(_cache.items()):
        for vname, val in list(context_chids.items()):
            out.append(" %s  %s  %i" % (vname,
                                        repr(isConnected(val['chid'])),
                                        context))
    out = strjoin('\n', out)
    if print_out:
        write(out)
    else:
        return out
    
## decorator functions for ca functionality:
#  decorator name      ensures before running decorated function:
#  --------------      -----------------------------------------------
#   withCA               libca is initialized 
#   withCHID             1st arg is a chid (dbr.chid_t)
#   withConnectedCHID    1st arg is a connected chid.
#
#  These tests are not rigorous CA tests (and ctypes.long is
#  accepted as a chid, connect_channel() is tried, but may fail)
##
def withCA(fcn):
    """decorator to ensure that libca and a context are created
    prior to function calls to the channel access library. This is
    intended for functions that at the startup of CA, such as
        create_channel

    Note that CA functions that take a Channel ID (chid) as an
    argument are  NOT wrapped by this: to get a chid, the
    library must have been initialized already."""
    def wrapper(*args, **kwds):
        "withCA wrapper"
        global libca
        if libca is None:
            initialize_libca()
        return fcn(*args, **kwds)
    wrapper.__doc__ = fcn.__doc__
    wrapper.__name__ = fcn.__name__
    wrapper.__dict__.update(fcn.__dict__)
    return wrapper

def withCHID(fcn):
    """decorator to ensure that first argument to a function
    is a chid. This performs a very weak test, as any ctypes
    long or python int will pass.

    It may be worth making a chid class (which could hold connection
    data of _cache) that could be tested here.  For now, that
    seems slightly 'not low-level' for this module.
    """
    def wrapper(*args, **kwds):
        "withCHID wrapper"
        if len(args)>0:
            chid = args[0]
            args = list(args)
            if isinstance(chid, int):
                args[0] = chid = dbr.chid_t(args[0])
            if not isinstance(chid, dbr.chid_t):
                raise ChannelAccessException(fcn.__name__,
                                             "not a valid chid %s %s args %s kwargs %s!" % (chid, type(chid), args, kwds))
        return fcn(*args, **kwds)
    wrapper.__doc__ = fcn.__doc__
    wrapper.__name__ = fcn.__name__
    wrapper.__dict__.update(fcn.__dict__)
    return wrapper


def withConnectedCHID(fcn):
    """decorator to ensure that first argument to a function is a
    chid that is actually connected. This will attempt to connect
    if needed."""
    def wrapper(*args, **kwds):
        "withConnectedCHID wrapper"
        if len(args)>0:
            chid = args[0]
            args = list(args)
            if isinstance(chid, int):
                args[0] = chid = dbr.chid_t(chid)
            if not isinstance(chid, dbr.chid_t):
                raise ChannelAccessException(fcn.__name__,
                                             "not a valid chid!")
            if not isConnected(chid):
                timeout = kwds.get('timeout', DEFAULT_CONNECTION_TIMEOUT)
                connect_channel(chid, timeout=timeout)
        return fcn(*args, **kwds)
    wrapper.__doc__ = fcn.__doc__
    wrapper.__name__ = fcn.__name__
    wrapper.__dict__.update(fcn.__dict__)
    return wrapper

def PySEVCHK(func_name, status, expected=dbr.ECA_NORMAL):
    """raise a ChannelAccessException if the wrapped
    status != ECA_NORMAL
    """
    if status == expected:
        return status
    raise CASeverityException(func_name, message(status))

def withSEVCHK(fcn):
    """decorator to raise a ChannelAccessException if the wrapped
    ca function does not return status=ECA_NORMAL
    """
    def wrapper(*args, **kwds):
        "withSEVCHK wrapper"
        status = fcn(*args, **kwds)
        return PySEVCHK( fcn.__name__, status)
    wrapper.__doc__ = fcn.__doc__
    wrapper.__name__ = fcn.__name__
    wrapper.__dict__.update(fcn.__dict__)
    return wrapper

##
## Event Handlers for get() event callbacks
def _onGetEvent(args):
    """Internal Event Handler for get events: not intended for use"""
    value = dbr.cast_args(args).contents
    # chid = dbr.chid_t(args.chid)
    pvname = name(args.chid)
    kwds = {'ftype':args.type, 'count':args.count,
           'chid':args.chid, 'pvname': pvname,
           'status':args.status}
    # add kwds arguments for CTRL and TIME variants
    if args.type >= dbr.CTRL_STRING:
        tmpv = value[0]
        for attr in dbr.ctrl_limits + ('precision', 'units', 'severity'):
            if hasattr(tmpv, attr):        
                kwds[attr] = getattr(tmpv, attr)
        if (hasattr(tmpv, 'strs') and hasattr(tmpv, 'no_str') and
            tmpv.no_str > 0):
            kwds['enum_strs'] = tuple([tmpv.strs[i].value for 
                                      i in range(tmpv.no_str)])

    elif args.type >= dbr.TIME_STRING:
        tmpv = value[0]
        kwds['status']    = tmpv.status
        kwds['severity']  = tmpv.severity
        kwds['timestamp'] = (dbr.EPICS2UNIX_EPOCH + tmpv.stamp.secs + 
                            1.e-6*int(tmpv.stamp.nsec/1000.00))
    nelem = args.count
    if args.type in (dbr.STRING, dbr.TIME_STRING, dbr.CTRL_STRING):
        nelem = dbr.MAX_STRING_SIZE
        
    value = _unpack(value, count=nelem, ftype=args.type)
    if hasattr(args.usr, '__call__'):
        args.usr(value=value, **kwds)

## connection event handler: 
def _onConnectionEvent(args):
    """set flag in cache holding whteher channel is
    connected. if provided, run a user-function"""
    ctx = current_context()
    pvname = name(args.chid)
    global _cache
    if ctx is None and len(_cache.keys()) > 0:
        ctx = _cache.keys()[0]
    if ctx not in _cache:
        _cache[ctx] = {}
    if pvname not in _cache[ctx]:
        _cache[ctx][pvname] = {'conn':False, 'chid': args.chid,
                               'ts':0, 'failures':0, 
                               'callbacks': []}

    conn = (args.op == dbr.OP_CONN_UP)
    entry = _cache[ctx][pvname]
    if isinstance(entry['chid'], dbr.chid_t) and  entry['chid'].value != args.chid:
        msg = 'Channel IDs do not match in connection callback (%s and %s)'
        raise ChannelAccessException('connect_channel',
                                     msg % (entry['chid'], args.chid))
    entry['conn'] = conn
    entry['chid'] = args.chid
    entry['ts']   = time.time()
    entry['failures'] = 0

    if len(entry.get('callbacks', [])) > 0:
        poll(evt=1.e-3, iot=10.0)
        for callback in entry.get('callbacks', []):
            if hasattr(callback, '__call__'):
                callback(pvname=pvname, 
                         chid=entry['chid'],
                         conn=entry['conn'])

    return 

## put event handler:
def _onPutEvent(args, **kwds):
    """set put-has-completed for this channel,
    call optional user-supplied callback"""
    pvname = name(args.chid)
    fcn  = _put_done[pvname][1]
    data = _put_done[pvname][2]
    _put_done[pvname] = (True, None, None)
    if hasattr(fcn, '__call__'):
        if isinstance(data, dict):
            kwds.update(data)
        elif data is not None:
            kwds['data'] = data
        fcn(pvname=pvname, **kwds)

# create global reference to these two callbacks
_CB_CONNECT = ctypes.CFUNCTYPE(None, dbr.connection_args)(_onConnectionEvent)
_CB_PUTWAIT = ctypes.CFUNCTYPE(None, dbr.event_handler_args)(_onPutEvent)  
_CB_EVENT   = ctypes.CFUNCTYPE(None, dbr.event_handler_args)(_onGetEvent)   

###
# 
# Now we're ready to wrap libca functions
#
###

# contexts
@withCA
@withSEVCHK
def context_create(context=None):
    "create a context -- context=1 for preemptive callbacks"
    if not PREEMPTIVE_CALLBACK:
        raise ChannelAccessException('context_create',
            'Cannot create new context with PREEMPTIVE_CALLBACK=False')
    if context is None:
        context = {False:0, True:1}[PREEMPTIVE_CALLBACK]
    return libca.ca_context_create(context)

@withCA
def context_destroy():
    "destroy current context"
    global _cache
    ctx = current_context() 
    ret = libca.ca_context_destroy()
    if ctx in _cache:
        for key in list(_cache[ctx].keys()):
            _cache[ctx].pop(key)
        _cache.pop(ctx)
    return ret
    
@withCA
def attach_context(context):
    "attach a context"        
    ret = libca.ca_attach_context(context) 
    return PySEVCHK('attach_context', ret, dbr.ECA_ISATTACHED)
        
@withCA
def detach_context():
    "detach a context"
    return libca.ca_detach_context()

@withCA
def replace_printf_handler(fcn=None):
    "replace printf output handler -- test???"
    if fcn is None:
        fcn = sys.stderr.write
    serr = ctypes.CFUNCTYPE(None, ctypes.c_char_p)(fcn)
    return libca.ca_replace_printf_handler(serr)

@withCA
def current_context():
    "return this context"
    return int(libca.ca_current_context())

@withCA
def client_status(context, level):
    "return status of client"
    return libca.ca_client_status(context, level)

@withCA
def flush_io():
    "i/o flush"
    return libca.ca_flush_io()

@withCA
def message(status):
    "write message"
    return BYTES2STR(libca.ca_message(status))

@withCA
def version():
    """return CA version"""
    return BYTES2STR(libca.ca_version())

@withCA
def pend_io(timeout=1.0):
    """polls CA for i/o. """    
    ret = libca.ca_pend_io(timeout)
    try:
        return PySEVCHK('pend_io', ret)
    except CASeverityException:
        return ret

## @withCA
def pend_event(timeout=1.e-4):
    """polls CA for events """    
    ret = libca.ca_pend_event(timeout)
    try:
        return PySEVCHK( 'pend_event', ret,  dbr.ECA_TIMEOUT)
    except CASeverityException:
        return ret

@withCA
def poll(evt=1.e-4, iot=1.0):
    """polls CA for events and i/o. """
    pend_event(evt)
    return pend_io(iot)    

@withCA
def test_io():
    """test if IO is complete: returns True if it is"""
    return (dbr.ECA_IODONE ==  libca.ca_test_io())

## create channel
@withCA
def create_channel(pvname, connect=False, callback=None):
    """ create a Channel for a given pvname

    connect=True will try to wait until connection is complete
    before returning

    a user-supplied callback function (callback) can be provided
    as a connection callback. This function will be called when
    the connection state changes, and will be passed these keyword
    arguments:
       pvname   name of PV
       chid     channel ID
       conn     connection state (True/False)

    If the channel is already connected for the PV name, the callback
    will be called immediately.
    """
    # 
    # Note that _CB_CONNECT (defined below) is a global variable, holding
    # a reference to _onConnectionEvent:  This is really the connection
    # callback that is run -- the callack here is stored in the _cache
    # and called by _onConnectionEvent.
    pvn = STR2BYTES(pvname)    
    ctx = current_context()
    global _cache
    if ctx not in _cache:
        _cache[ctx] = {}
    if pvname not in _cache[ctx]: # new PV for this context
        entry = {'conn':False,  'chid': None, 
                 'ts': 0,  'failures':0, 
                 'callbacks': [ callback ]}
        _cache[ctx][pvname] = entry
    else:
        entry = _cache[ctx][pvname]
        if not entry['conn'] and callback is not None: # pending connection
            _cache[ctx][pvname]['callbacks'].append(callback)
        elif (hasattr(callback, '__call__') and 
              not callback in entry['callbacks']):
            entry['callbacks'].append(callback)
            callback(chid=entry['chid'], conn=entry['conn'])

    if entry.get('chid', None) is not None:
        # already have or waiting on a chid
        chid = _cache[ctx][pvname]['chid']
    else:
        chid = dbr.chid_t()
        ret = libca.ca_create_channel(pvn, _CB_CONNECT, 0, 0,
                                  ctypes.byref(chid))
        PySEVCHK('create_channel', ret)
        entry['chid'] = chid

    if connect:
        connect_channel(chid)
    poll()
    return chid

@withCHID
def connect_channel(chid, timeout=None, verbose=False):
    """ wait (up to timeout) until a chid is connected

    Normally, channels will connect very fast, and the
    connection callback will succeed the first time.

    For un-connected Channels (that are nevertheless queried),
    the 'ts' (timestamp of last connecion attempt) and
    'failures' (number of failed connection attempts) from
    the _cache will be used to prevent spending too much time
    waiting for a connection that may never happen.
    
    """
    if verbose:
        write(' connect channel -> %s %s %s ' %
               (repr(chid), repr(state(chid)), repr(dbr.CS_CONN)))
    conn = (state(chid) == dbr.CS_CONN)
    if not conn:
        # not connected yet, either indicating a slow network
        # or a truly un-connnectable channel.
        start_time = time.time()
        ctx = current_context()
        pvname = name(chid)
        global _cache
        if ctx not in _cache:
            _cache[ctx] = {}

        if timeout is None:
            timeout = DEFAULT_CONNECTION_TIMEOUT
        while (not conn and ((time.time()-start_time) < timeout)):
            poll()
            conn = (state(chid) == dbr.CS_CONN)
        if not conn:
            _cache[ctx][pvname]['ts'] = time.time()
            _cache[ctx][pvname]['failures'] += 1
    return conn

# functions with very light wrappings:
@withCHID
def name(chid):
    "channel name"
    return BYTES2STR(libca.ca_name(chid))

@withCHID
def host_name(chid):
    "channel host name"
    return BYTES2STR(libca.ca_host_name(chid))

@withCHID
def element_count(chid):
    "channel data size -- element count"
    return libca.ca_element_count(chid)

@withCHID
def read_access(chid):
    "read access for channel"
    return libca.ca_read_access(chid)

@withCHID
def write_access(chid):
    "write access for channel"    
    return libca.ca_write_access(chid)

@withCHID
def field_type(chid):
    "integer giving data type for channel"
    return libca.ca_field_type(chid)

@withCHID
def clear_channel(chid):
    "clear channel"    
    return libca.ca_clear_channel(chid)

@withCHID
def state(chid):
    "read attachment state for channel"
    return libca.ca_state(chid)

def isConnected(chid):
    "return whether channel is connected"
    return dbr.CS_CONN == state(chid)

def access(chid):
    "string description of access"
    acc = read_access(chid) + 2 * write_access(chid)
    return ('no access', 'read-only', 'write-only', 'read/write')[acc]

def promote_type(chid, use_time=False, use_ctrl=False):
    "promote native field type to TIME or CTRL variant"
    ftype = field_type(chid)
    if   use_ctrl:
        ftype += dbr.CTRL_STRING 
    elif use_time:
        ftype += dbr.TIME_STRING 
    if ftype == dbr.CTRL_STRING:
        ftype = dbr.TIME_STRING
    return ftype

def native_type(ftype):
    "return native field type from TIME or CTRL variant"
    if ftype == dbr.CTRL_STRING:
        ftype = dbr.TIME_STRING
    ntype = ftype
    if ftype > dbr.CTRL_STRING:
        ntype -= dbr.CTRL_STRING
    elif ftype >= dbr.TIME_STRING:
        ntype -= dbr.TIME_STRING
    return ntype

def _unpack(data, count=None, chid=None, ftype=None, as_numpy=True):
    """unpack raw data returned from an array get or
    subscription callback"""
    def unpack_simple(data, count, ntype, use_numpy):
        "simple, native data type"
        if count == 1 and ntype != dbr.STRING:
            return data[0]
        if ntype == dbr.STRING:
            out = []
            for elem in range(min(count, len(data))):
                this = strjoin('', data[elem]).rstrip()
                if '\x00' in this:
                    this = this[:this.index('\x00')]
                out.append(this)

            if len(out) == 1:
                return out[0]
            else:
                return out
        # waveform data:
        if ntype == dbr.CHAR:
            if use_numpy:
                data = numpy.array(data)
            return copy.copy(data[:])
        elif use_numpy:
            return copy.copy(numpy.ctypeslib.as_array(data))
        return list(data)
        
    def unpack_ctrltime(data, count, ntype, use_numpy):
        "ctrl and time data types"
        if count == 1 or ntype == dbr.STRING:
            out = data[0].value
            if ntype == dbr.STRING and '\x00' in out:
                out = out[:out.index('\x00')]
            return out
        # fix for CTRL / TIME array data:Thanks to Glen Wright !
        out = (count*dbr.Map[ntype]).from_address(ctypes.addressof(data) +
                                                  dbr.value_offset[ftype])

        if ntype == dbr.CHAR:
            out = copy.copy(out)
        if use_numpy:
            return copy.copy(numpy.ctypeslib.as_array(out))
        return list(out)

    unpack = unpack_simple
    if ftype >= dbr.TIME_STRING:
        unpack = unpack_ctrltime

    if count is None and chid is not None:
        count = element_count(chid)
    if count is None:
        count = 1

    if ftype is None and chid is not None:
        ftype = field_type(chid)
    if ftype is None:
        ftype = dbr.INT

    ntype = native_type(ftype)
    use_numpy = (count > 1 and HAS_NUMPY and as_numpy and
                 ntype != dbr.STRING)
    return unpack(data, count, ntype, use_numpy)

@withConnectedCHID
def get(chid, ftype=None, as_string=False, count=None, as_numpy=True):
    """return the current value for a Channel.  Options are
       ftype       field type to use (native type is default)
       as_string   flag(True/False) to get a string representation
                   of the value returned.  This is not nearly as
                   featured as for a PV -- see pv.py for more details.
       as_numpy    flag(True/False) to use numpy array as the
                   return type for array data.       
    """
    if ftype is None:
        ftype = field_type(chid)
    if ftype in (None, -1):
        return
    if count is None:
        count = element_count(chid)
    else:
        count = min(count, element_count(chid))
       
    data = (count*dbr.Map[ftype])()

    ret = libca.ca_array_get(ftype, count, chid, data)
    PySEVCHK('get', ret)
    poll()
    if count > 2:
        tcount = min(count, 1000)
        poll(evt=tcount*1.e-5, iot=tcount*0.01)

    val = _unpack(data, count=count, ftype=ftype, as_numpy=as_numpy)
    if as_string:
        val = _as_string(val, chid, count, ftype)
    return val

def _as_string(val, chid, count, ftype):
    "primitive conversion of value to a string"
    try:
        if (ftype in (dbr.CHAR, dbr.TIME_CHAR, dbr.CTRL_CHAR) and
            count < AUTOMONITOR_MAXLENGTH):
            val = strjoin('',   [chr(i) for i in val if i>0]).strip()
        elif ftype == dbr.ENUM and count == 1:
            val = get_enum_strings(chid)[val]
        elif count > 1:
            val = '<array count=%d, type=%d>' % (count, ftype)
        val = str(val)
    except ValueError:
        pass            
    return val
                    
@withConnectedCHID
def put(chid, value, wait=False, timeout=30, callback=None,
        callback_data=None):
    """put value to a Channel, with optional wait and
    user-defined callback.  Arguments:
       chid      channel id (required)
       value     value to put to Channel (required)
       wait      Flag for whether to block here while put
                 is processing.  Default = False
       timeout   maximum time to wait for a blocking put.
       callback  user-defined to be called when put has
                 finished processing.
       callback_data  data passed on to user-defined callback

    Specifying a callback does NOT require a blocking put().  
    
    returns 1 on sucess and -1 on timed-out
    """
    ftype = field_type(chid)
    count = element_count(chid)
    data  = (count*dbr.Map[ftype])()    

    if ftype == dbr.STRING:
        if count == 1:
            data[0].value = value
        else:
            for elem in range(min(count, len(value))):
                data[elem].value = value[elem]
    elif count == 1:
        try:
            data[0] = value
        except TypeError:
            data[0] = type(data[0])(value)
        except:
            errmsg = "Cannot put value '%s' to PV of type '%s'"
            tname  = dbr.Name(ftype).lower()
            raise ChannelAccessException('put', \
                                         errmsg % (repr(value),tname))
    else:
        # auto-convert strings to arrays for character waveforms
        # could consider using
        # numpy.fromstring(("%s%s" % (s,'\x00'*maxlen))[:maxlen],
        #                  dtype=numpy.uint8)
        if ftype == dbr.CHAR and isinstance(value, str):
            pad = '\x00'*(1+count-len(value))
            value = [ord(i) for i in ("%s%s" % (value, pad))[:count]]
        try:
            ndata, nuser = len(data), len(value)
            if nuser > ndata:
                value = value[:ndata]
            data[:len(value)] = list(value)
        except (ValueError, IndexError):
            errmsg = "Cannot put array data to PV of type '%s'"            
            raise ChannelAccessException('put', errmsg % (repr(value)))
      
    # simple put, without wait or callback
    if not (wait or hasattr(callback, '__call__')):
        ret =  libca.ca_array_put(ftype, count, chid, data)
        PySEVCHK('put', ret)
        poll()
        return ret
    # wait with wait or callback    # wait with wait or callback
    pvname = name(chid)
    _put_done[pvname] = (False, callback, callback_data)
    ret = libca.ca_array_put_callback(ftype, count, chid,
                                      data, _CB_PUTWAIT, 0)
    PySEVCHK('put', ret)
    poll(evt=1.e-4, iot=0.05)
    if wait:
        start_time, finished = time.time(), False
        while not finished:
            poll()
            finished = (_put_done[pvname][0] or
                        (time.time()-start_time) > timeout)
        if not _put_done[pvname][0]:
            ret = -ret
    return ret

@withConnectedCHID
def get_ctrlvars(chid):
    """return the CTRL fields for a Channel.  Depending on 
    the native type, these fields may include
        status  severity precision  units  enum_strs
        upper_disp_limit     lower_disp_limit
        upper_alarm_limit    lower_alarm_limit
        upper_warning_limit  lower_warning_limit
        upper_ctrl_limit    lower_ctrl_limit
        
    note (difference with C lib): enum_strs will be a
    list of strings for the names of ENUM states.
    
    """
    ftype = promote_type(chid, use_ctrl=True)
    dat = (1*dbr.Map[ftype])()

    ret = libca.ca_array_get(ftype, 1, chid, dat)
    PySEVCHK('get_ctrlvars', ret)
    poll()
    out = {}
    tmpv = dat[0]
    for attr in ('precision', 'units', 'severity', 'status',
                 'upper_disp_limit', 'lower_disp_limit',
                 'upper_alarm_limit', 'upper_warning_limit',
                 'lower_warning_limit','lower_alarm_limit',
                 'upper_ctrl_limit', 'lower_ctrl_limit'):
        if hasattr(tmpv, attr):
            out[attr] = getattr(tmpv, attr)
    if (hasattr(tmpv, 'strs') and hasattr(tmpv, 'no_str') and
        tmpv.no_str > 0):
        out['enum_strs'] = tuple([BYTES2STR(tmpv.strs[i].value)
                                  for i in range(tmpv.no_str)])
    return out

@withConnectedCHID
def get_timevars(chid):
    """return the TIME fields for a Channel.  Depending on 
    the native type, these fields may include
        status  severity timestamp
    """
    ftype = promote_type(chid, use_time=True)
    dat = (1*dbr.Map[ftype])()

    ret = libca.ca_array_get(ftype, 1, chid, dat)
    PySEVCHK('get_timevars', ret)
    poll()
    out = {}
    val = dat[0]
    for attr in ('status', 'severity', 'timestamp'):
        if hasattr(val, attr):
            out[attr] = getattr(val, attr)
    return out

def get_timestamp(chid):
    """return the timestamp of a Channel."""
    return get_timevars(chid).get('timestamp', 0)

def get_severity(chid):
    """return the severity of a Channel."""
    return get_timevars(chid).get('severity', 0)

def get_precision(chid):
    """return the precision of a Channel.  For Channels with
    native type other than FLOAT or DOUBLE, this will be 0"""
    if field_type(chid) in (dbr.FLOAT, dbr.DOUBLE):
        return get_ctrlvars(chid).get('precision', 0)
    return 0

def get_enum_strings(chid):
    """return list of names for ENUM states of a Channel.  Returns
    None for non-ENUM Channels"""
    if field_type(chid) == dbr.ENUM:
        return get_ctrlvars(chid).get('enum_strs', None)
    return None

@withConnectedCHID
def create_subscription(chid, use_time=False, use_ctrl=False,
                        mask=7, callback=None):
    """
    setup a callback function to be called when a PVs value or state changes.

    Important Note:
        KEEP The returned tuple in named variable: if the return argument
        gets garbage collected, a coredump will occur.
    
    """
    ftype = promote_type(chid, use_ctrl=use_ctrl, use_time=use_time)
    count = element_count(chid)

    uarg  = ctypes.py_object(callback)
    evid  = ctypes.c_void_p()
    poll()
    ret = libca.ca_create_subscription(ftype, 0, chid, mask,
                                       _CB_EVENT, uarg, ctypes.byref(evid))
    PySEVCHK('create_subscription', ret)
    
    poll()
    return (_CB_EVENT, uarg, evid)

@withCA
@withSEVCHK
def clear_subscription(evid):
    "cancel subscription"
    return libca.ca_clear_subscription(evid)


@withCA
@withSEVCHK
def sg_block(gid, timeout=10.0):
    "sg block"
    return libca.ca_sg_block(gid, timeout)

@withCA
def sg_create():
    "sg create"
    gid  = ctypes.c_ulong()
    pgid = ctypes.pointer(gid)
    ret =  libca.ca_sg_create(pgid)
    PySEVCHK('sg_create', ret)
    return gid

@withCA
@withSEVCHK
def sg_delete(gid):
    "sg delete"
    return libca.ca_sg_delete(gid)

@withCA
def sg_test(gid):
    "sg test"
    ret = libca.ca_sg_test(gid)
    return PySEVCHK('sg_test', ret, dbr.ECA_IODONE)

@withCA
@withSEVCHK
def sg_reset(gid):
    "sg reset"
    return libca.ca_sg_reset(gid)

def sg_get(gid, chid, ftype=None, as_numpy=True, as_string=True):
    """synchronous-group get of the current value for a Channel.
    same options as get()
    
    Note that the returned tuple from a sg_get() will have to be
    unpacked with the '_unpack' method:

    >>> chid = epics.ca.create_channel(PV_Name)
    >>> epics.ca.connect_channel(chid1)
    >>> sg = epics.ca.sg_create() 
    >>> data = epics.ca.sg_get(sg, chid)
    >>> epics.ca.sg_block(sg)
    >>> print epics.ca._unpack(data, chid=chid)
    """
    if not isinstance(chid, dbr.chid_t):
        raise ChannelAccessException('sg_get', "not a valid chid!")

    if ftype is None:
        ftype = field_type(chid)
    count = element_count(chid)

    data = (count*dbr.Map[ftype])()
    ret = libca.ca_sg_array_get(gid, ftype, count, chid, data)
    PySEVCHK('sg_get', ret)
    poll()

    val = _unpack(data, count=count, ftype=ftype, as_numpy=as_numpy)
    if as_string:
        val = _as_string(val, chid, count, ftype)
    return val
 
def sg_put(gid, chid, value):
    "synchronous-group put: cannot wait or get callback!"
    if not isinstance(chid, dbr.chid_t):
        raise ChannelAccessException('sg_put', "not a valid chid!")

    ftype = field_type(chid)
    count = element_count(chid)
    data  = (count*dbr.Map[ftype])()    

    if ftype == dbr.STRING:
        if count == 1:
            data[0].value = value
        else:
            for elem in range(min(count, len(value))):
                data[elem].value = value[elem]
    elif count == 1:
        try:
            data[0] = value
        except TypeError:
            data[0] = type(data[0])(value)
        except:
            errmsg = "Cannot put value '%s' to PV of type '%s'"
            tname   = dbr.Name(ftype).lower()
            raise ChannelAccessException('put', \
                                         errmsg % (repr(value),tname))
    else:
        # auto-convert strings to arrays for character waveforms
        # could consider using
        # numpy.fromstring(("%s%s" % (s,'\x00'*maxlen))[:maxlen],
        #                  dtype=numpy.uint8)
        if ftype == dbr.CHAR and isinstance(value, str):
            pad = '\x00'*(1+count-len(value))
            value = [ord(i) for i in ("%s%s" % (value, pad))[:count]]
        try:
            ndata = len(data)
            nuser = len(value)
            if nuser > ndata:
                value = value[:ndata]
            data[:len(value)] = list(value)
        except:
            errmsg = "Cannot put array data to PV of type '%s'"            
            raise ChannelAccessException('put', errmsg % (repr(value)))
      
    ret =  libca.ca_sg_array_put(gid, ftype, count, chid, data)
    PySEVCHK('sg_put', ret)
    # poll()
    return ret
