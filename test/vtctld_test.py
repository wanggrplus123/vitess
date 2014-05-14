#!/usr/bin/env python
import json
import logging
import os
import unittest
import urllib2

import environment
import tablet
import utils


# range "" - 80
shard_0_master = tablet.Tablet()
shard_0_replica = tablet.Tablet()
shard_0_spare = tablet.Tablet()
# range 80 - ""
shard_1_master = tablet.Tablet()
shard_1_replica = tablet.Tablet()
# not assigned
idle = tablet.Tablet()
scrap = tablet.Tablet()
# all tablets
tablets = [shard_0_master, shard_0_replica, shard_1_master, shard_1_replica,
           idle, scrap, shard_0_spare]


class VtctldError(Exception): pass


class Vtctld(object):

  def __init__(self):
    self.port = environment.reserve_ports(1)

  def dbtopo(self):
    data = json.load(urllib2.urlopen('http://localhost:%u/dbtopo?format=json' %
                                     self.port))
    if data["Error"]:
      raise VtctldError(data)
    return data["Topology"]

  def serving_graph(self):
    data = json.load(urllib2.urlopen('http://localhost:%u/serving_graph/test_nj?format=json' % self.port))
    if data["Error"]:
      raise VtctldError(data)
    return data["ServingGraph"]["Keyspaces"]

  def start(self):
    args = [environment.binary_path('vtctld'),
            '-debug',
            '-templates', environment.vttop + '/go/cmd/vtctld/templates',
            '-log_dir', environment.vtlogroot,
            '-port', str(self.port),
            ] + \
            environment.topo_server_flags() + \
            environment.tablet_manager_protocol_flags()
    stderr_fd = open(os.path.join(environment.tmproot, "vtctld.stderr"), "w")
    self.proc = utils.run_bg(args, stderr=stderr_fd)
    return self.proc

  def process_args(self):
    return ['-vtctld_addr', 'http://localhost:%u/' % self.port]


vtctld = Vtctld()


def setUpModule():
  try:
    environment.topo_server_setup()

    setup_procs = [t.init_mysql() for t in tablets]
    utils.wait_procs(setup_procs)
    vtctld.start()

  except:
    tearDownModule()
    raise


def tearDownModule():
  if utils.options.skip_teardown:
    return

  teardown_procs = [t.teardown_mysql() for t in tablets]
  utils.wait_procs(teardown_procs, raise_on_error=False)

  environment.topo_server_teardown()
  utils.kill_sub_processes()
  utils.remove_tmp_files()

  for t in tablets:
    t.remove_tree()


class TestVtctld(unittest.TestCase):

  @classmethod
  def setUpClass(klass):
    utils.run_vtctl('CreateKeyspace test_keyspace')

    shard_0_master.init_tablet( 'master',  'test_keyspace', '-80')
    shard_0_replica.init_tablet('spare', 'test_keyspace', '-80')
    shard_0_spare.init_tablet('spare', 'test_keyspace', '-80')
    shard_1_master.init_tablet( 'master',  'test_keyspace', '80-')
    shard_1_replica.init_tablet('replica', 'test_keyspace', '80-')
    idle.init_tablet('idle')
    scrap.init_tablet('idle')

    utils.run_vtctl('RebuildKeyspaceGraph test_keyspace', auto_log=True)

    for t in [shard_0_master, shard_1_master, shard_1_replica]:
      t.create_db('vt_test_keyspace')
      t.start_vttablet(extra_args=vtctld.process_args())
    shard_0_replica.create_db('vt_test_keyspace')
    shard_0_replica.start_vttablet(extra_args=vtctld.process_args(),
                                   target_tablet_type='replica',
                                   wait_for_state='NOT_SERVING')

    for t in scrap, idle, shard_0_spare:
      t.start_vttablet(wait_for_state='NOT_SERVING',
                       extra_args=vtctld.process_args())

    scrap.scrap()

    for t in [shard_0_master, shard_0_replica, shard_0_spare,
              shard_1_master, shard_1_replica, idle, scrap]:
      t.reset_replication()
    utils.run_vtctl('ReparentShard -force test_keyspace/-80 ' +
                    shard_0_master.tablet_alias, auto_log=True)
    utils.run_vtctl('ReparentShard -force test_keyspace/80- ' +
                    shard_1_master.tablet_alias, auto_log=True)
    shard_0_replica.wait_for_vttablet_state('SERVING')

    # run checks now before we start the tablets
    utils.validate_topology()

  def setUp(self):
    self.data = vtctld.dbtopo()
    self.serving_data = vtctld.serving_graph()

  def test_assigned(self):
    logging.debug("test_assigned: %s", str(self.data))
    self.assertItemsEqual(self.data["Assigned"].keys(), ["test_keyspace"])
    self.assertItemsEqual(self.data["Assigned"]["test_keyspace"].keys(),
                          ["-80", "80-"])

  def test_not_assigned(self):
    self.assertEqual(len(self.data["Idle"]), 1)
    self.assertEqual(len(self.data["Scrap"]), 1)

  def test_partial(self):
    utils.pause("You can now run a browser and connect to http://localhost:%u to manually check topology" % vtctld.port)
    self.assertEqual(self.data["Partial"], True)

  def test_explorer_redirects(self):
    base = 'http://localhost:%u' % vtctld.port
    self.assertEqual(urllib2.urlopen(base + '/explorers/redirect?type=keyspace&explorer=zk&keyspace=test_keyspace').geturl(),
                     base + '/zk/global/vt/keyspaces/test_keyspace')
    self.assertEqual(urllib2.urlopen(base + '/explorers/redirect?type=shard&explorer=zk&keyspace=test_keyspace&shard=-80').geturl(),
                     base + '/zk/global/vt/keyspaces/test_keyspace/shards/-80')
    self.assertEqual(urllib2.urlopen(base + '/explorers/redirect?type=tablet&explorer=zk&alias=%s' % shard_0_replica.tablet_alias).geturl(),
                     base + shard_0_replica.zk_tablet_path)

    self.assertEqual(urllib2.urlopen(base + '/explorers/redirect?type=srv_keyspace&explorer=zk&keyspace=test_keyspace&cell=test_nj').geturl(),
                     base + '/zk/test_nj/vt/ns/test_keyspace')
    self.assertEqual(urllib2.urlopen(base + '/explorers/redirect?type=srv_shard&explorer=zk&keyspace=test_keyspace&shard=-80&cell=test_nj').geturl(),
                     base + '/zk/test_nj/vt/ns/test_keyspace/-80')
    self.assertEqual(urllib2.urlopen(base + '/explorers/redirect?type=srv_type&explorer=zk&keyspace=test_keyspace&shard=-80&tablet_type=replica&cell=test_nj').geturl(),
                     base + '/zk/test_nj/vt/ns/test_keyspace/-80/replica')

    self.assertEqual(urllib2.urlopen(base + '/explorers/redirect?type=replication&explorer=zk&keyspace=test_keyspace&shard=-80&cell=test_nj').geturl(),
                     base + '/zk/test_nj/vt/replication/test_keyspace/-80')

  def test_serving_graph(self):
    self.assertItemsEqual(self.serving_data.keys(), ["test_keyspace"])
    self.assertItemsEqual(self.serving_data["test_keyspace"].keys(),
                          ["-80", "80-"])
    self.assertItemsEqual(self.serving_data["test_keyspace"]["-80"].keys(),
                          ["master", "replica"])
    self.assertEqual(len(self.serving_data["test_keyspace"]["-80"]["master"]),
                     1)

  def test_tablet_status(self):
    # the vttablet that has a health check has a bit more, so using it
    shard_0_replica_status = shard_0_replica.get_status()
    self.assertIn('Polling health information from MySQLReplicationLag(allowedLag=30)', shard_0_replica_status)
    self.assertIn('Alias: <a href="http://localhost:', shard_0_replica_status)
    self.assertIn('</html>', shard_0_replica_status)

if __name__ == '__main__':
  utils.main()
