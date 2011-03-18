#!/usr/bin/env python
# test expression parsing

import os
import sys
import time
import unittest

from epics import ca, dbr

import pvnames

def _ca_connect(chid,timeout=5.0):
    n  = 0
    t0 = time.time()
    conn = 2==ca.state(chid)
    while (not conn) and (time.time()-t0 < timeout):
        ca.poll(1.e-6,1.e-4)
        conn = 2==ca.state(chid)
        n += 1
    return conn, time.time()-t0, n
    
def write(msg):
    sys.stdout.write(msg)
    sys.stdout.flush()

CONN_DAT ={}
CHANGE_DAT = {}


def onConnect(pvname=None, conn=None, chid=None,  **kws):
    write('  /// Connection status changed:  %s  %s\n' % (pvname, repr(kws)))
    global CONN_DAT
    CONN_DAT[pvname] = conn
    
def onChanges(pvname=None, value=None, **kws):
    write( '/// New Value: %s  value=%s, kw=%s\n' %( pvname, str(value), repr(kws)))
    global CHANGE_DAT
    CHANGE_DAT[pvname] = value

class CA_BasicTests(unittest.TestCase):
    def testA_CreateChid(self):
        write('Simple Test: create chid\n')
        chid = ca.create_channel(pvnames.double_pv)
        self.assertNotEqual(chid,None)

    def testA_CreateChidWithConn(self):
        write('Simple Test: create chid with conn callback\n')
        chid = ca.create_channel(pvnames.int_pv,
                                 callback=onConnect)
        val = ca.get(chid)
        
        global CONN_DAT
        conn = CONN_DAT.get(pvnames.int_pv, None)
        self.assertEqual(conn, True)
        
    def test_dbrName(self):
        write( 'DBR Type Check\n')
        self.assertEqual(dbr.Name(dbr.STRING), 'STRING')
        self.assertEqual(dbr.Name(dbr.FLOAT),  'FLOAT')
        self.assertEqual(dbr.Name(dbr.ENUM), 'ENUM')
        self.assertEqual(dbr.Name(dbr.CTRL_CHAR), 'CTRL_CHAR')
        self.assertEqual(dbr.Name(dbr.TIME_DOUBLE), 'TIME_DOUBLE')
        self.assertEqual(dbr.Name(dbr.TIME_LONG), 'TIME_LONG')

        self.assertEqual(dbr.Name('STRING', reverse=True), dbr.STRING)
        self.assertEqual(dbr.Name('DOUBLE', reverse=True), dbr.DOUBLE)
        self.assertEqual(dbr.Name('CTRL_ENUM', reverse=True), dbr.CTRL_ENUM)
        self.assertEqual(dbr.Name('TIME_LONG', reverse=True), dbr.TIME_LONG)
        
    def test_Connect1(self):
        chid = ca.create_channel(pvnames.double_pv)
        conn,dt,n = _ca_connect(chid, timeout=2)
        write( 'CA Connection Test1: connect to existing PV\n')
        write( ' connected in %.4f sec\n' % (dt))
        self.assertEqual(conn,True)

    def test_Connected(self):
        pvn = pvnames.double_pv
        chid = ca.create_channel(pvn,connect=True)
        isconn = ca.isConnected(chid)
        write( 'CA test Connected (%s) = %s\n' % (pvn,isconn))
        self.assertEqual(isconn,True)
        state= ca.state(chid)
        self.assertEqual(state,ca.dbr.CS_CONN)
        acc = ca.access(chid)
        self.assertEqual(acc,'read/write')


    def test_DoubleVal(self):
        pvn = pvnames.double_pv
        chid = ca.create_channel(pvn,connect=True)
        cdict  = ca.get_ctrlvars(chid)
        write( 'CA testing CTRL Values for a Double (%s)\n'   % (pvn))
        self.failUnless('units' in cdict)
        self.failUnless('precision' in cdict)
        self.failUnless('severity' in cdict)
       
        hostname = ca.host_name(chid)
        self.failUnless(hostname.startswith(pvnames.double_pv_host))

        count = ca.element_count(chid)
        self.assertEqual(count,1)

        ftype= ca.field_type(chid)
        self.assertEqual(ftype,ca.dbr.DOUBLE)

        prec = ca.get_precision(chid)
        self.assertEqual(prec, pvnames.double_pv_prec)

        units= ca.get_ctrlvars(chid)['units']
        self.assertEqual(units, pvnames.double_pv_units)

    def test_UnConnected(self):
        write( 'CA Connection Test1: connect to non-existing PV (2sec timeout)\n')
        chid = ca.create_channel('impossible_pvname_certain_to_fail')
        conn,dt,n = _ca_connect(chid, timeout=2)
        self.assertEqual(conn,False)


    def test_promote_type(self):
        pvn = pvnames.double_pv
        chid = ca.create_channel(pvn,connect=True)
        write( 'CA promote type (%s)\n' % (pvn))
        f_t  = ca.promote_type(chid,use_time=True)
        f_c  = ca.promote_type(chid,use_ctrl=True)        
        self.assertEqual(f_t, ca.dbr.TIME_DOUBLE)
        self.assertEqual(f_c, ca.dbr.CTRL_DOUBLE)

    def test_Enum(self):
        pvn  = pvnames.enum_pv
        chid = ca.create_channel(pvn,connect=True)
        write( 'CA test Enum (%s)\n' % (pvn))
        enumstrs = ca.get_enum_strings(chid)
        self.failUnless(len(enumstrs)>1)

        self.failUnless(isinstance(enumstrs[0],str))
        write( 'CA EnumStrings (%s) = %s\n' % (pvn,repr(enumstrs)))
        self.failUnless(enumstrs,pvnames.enum_pv_strs)


    def test_subscription_1(self):
        pvn = pvnames.updating_pv1
        chid = ca.create_channel(pvn,connect=True)
        eventID = ca.create_subscription(chid, callback=onChanges)

        start_time = time.time()
        global CHANGE_DAT
        while time.time()-start_time < 5.0:
            time.sleep(0.01)
            if CHANGE_DAT.get(pvn, None) is not None:
                break
        val = CHANGE_DAT.get(pvn, None)
        self.assertNotEqual(val, None)


    def test_subscription_2(self):
        pvn = pvnames.updating_str1
        chid = ca.create_channel(pvn,connect=True)
        eventID = ca.create_subscription(chid, callback=onChanges)

        start_time = time.time()
        global CHANGE_DAT
        while time.time()-start_time < 10.0:
            time.sleep(0.01)
            ca.put(chid, "%.1f" % (time.time()-start_time) )
            if CHANGE_DAT.get(pvn, None) is not None:
                break
        val = CHANGE_DAT.get(pvn, None)
        self.assertNotEqual(val, None)


    def test_Values(self):
        write( 'CA test Values (compare 6 values with caget)\n')
        os.system('rm ./caget.tst')
        vals = {}
        for pvn in (pvnames.str_pv,  pvnames.int_pv,
                    pvnames.float_pv, pvnames.enum_pv,
                    pvnames.long_pv,  pvnames.double_pv,
                    ):
            os.system('caget  -n -f5 %s >> ./caget.tst' % pvn)
            chid = ca.create_channel(pvn)
            ca.connect_channel(chid)
            vals[pvn] = ca.get(chid)
        rlines = open('./caget.tst', 'r').readlines()
        for line in rlines:
            pvn, sval = [i.strip() for i in line[:-1].split(' ', 1)]
            tval = str(vals[pvn])
            if pvn in (pvnames.float_pv,pvnames.double_pv): # use float precision!
                tval = "%.5f" % vals[pvn]
            self.assertEqual(tval, sval)
        

if __name__ == '__main__':
    suite = unittest.TestLoader().loadTestsFromTestCase( CA_BasicTests)
    unittest.TextTestRunner(verbosity=1).run(suite)
