#!/usr/bin/env python
# -*- coding: utf-8 -*-


import os
import sys
import time
import libvirt
import json
import jimit as ji
import xml.etree.ElementTree as ET

from jimvn_exception import ConnFailed

from initialize import config, logger, r, emit
from guest import Guest
from utils import Utils


__author__ = 'James Iter'
__date__ = '2017/3/1'
__contact__ = 'james.iter.cn@gmail.com'
__copyright__ = '(c) 2017 by James Iter.'


class Host(object):
    def __init__(self):
        self.conn = None
        self.dirty_scene = False
        self.guest = None
        self.guest_mapping_by_uuid = dict()
        self.hostname = ji.Common.get_hostname()

    def init_conn(self):
        self.conn = libvirt.open()

        if self.conn is None:
            raise ConnFailed(u'打开连接失败 --> ' + sys.stderr)

    def refresh_guest_mapping(self):
        for guest in self.conn.listAllDomains():
            self.guest_mapping_by_uuid[guest.UUIDString()] = guest

    def clear_scene(self):

        if self.dirty_scene:

            if self.guest.gf.exists(self.guest.guest_dir):
                self.guest.gf.rmtree(self.guest.guest_dir)

            else:
                log = u'清理现场失败: 不存在的路径 --> ' + self.guest.guest_dir
                logger.warn(msg=log)
                emit.warn(msg=log)

            self.dirty_scene = False

    def create_guest_engine(self):
        while True:
            if Utils.exit_flag:
                Utils.thread_counter -= 1
                print 'Thread create_guest_engine say bye-bye'
                return

            try:
                # 清理上个周期弄脏的现场
                self.clear_scene()
                # 取系统最近 5 分钟的平均负载值
                load_avg = os.getloadavg()[1]
                # sleep 加 1，避免 load_avg 为 0 时，循环过度
                time.sleep(load_avg * 10 + 1)
                print 'create_guest_engine alive: ' + ji.JITime.gmt(ts=time.time())

                # 大于 0.6 的系统将不再被分配创建虚拟机
                if load_avg > 0.6:
                    continue

                msg = r.rpop(config['vm_create_queue'])
                if msg is None:
                    continue

                try:
                    msg = json.loads(msg)
                except ValueError as e:
                    logger.error(e.message)
                    emit.emit(e.message)
                    continue

                self.guest = Guest(uuid=msg['uuid'], name=msg['name'], glusterfs_volume=msg['glusterfs_volume'],
                                   template_path=msg['template_path'], disks=msg['guest_disks'],
                                   writes=msg['writes'], xml=msg['xml'])
                if Guest.gf is None:
                    Guest.glusterfs_volume = msg['glusterfs_volume']
                    Guest.init_gfapi()

                self.guest.generate_guest_dir()

                # 虚拟机基础环境路径创建后，至虚拟机定义成功前，认为该环境是脏的
                self.dirty_scene = True

                if not self.guest.generate_system_image():
                    continue

                # 由该线程最顶层的异常捕获机制，处理其抛出的异常
                self.guest.init_config()

                if not self.guest.generate_disk_image():
                    continue

                if not self.guest.define_by_xml(conn=self.conn):
                    continue

                # 虚拟机定义成功后，该环境由脏变为干净，重置该变量为 False，避免下个周期被清理现场
                self.dirty_scene = False

                if not self.guest.start_by_uuid(conn=self.conn):
                    # 不清理现场，如需清理，让用户手动通过面板删除
                    continue

            except Exception as e:
                logger.error(e.message)
                emit.error(e.message)

    # TODO: 解决多线程访问 self.guest 问题
    def guest_operate_engine(self):

        ps = r.pubsub(ignore_subscribe_messages=False)
        ps.subscribe(config['instruction_channel'])

        while True:
            if Utils.exit_flag:
                Utils.thread_counter -= 1
                print 'Thread guest_operate_engine say bye-bye'
                return

            try:
                msg = ps.get_message(timeout=1)
                print 'guest_operate_engine alive: ' + ji.JITime.gmt(ts=time.time())
                if msg is None:
                    continue

                try:
                    msg = json.loads(msg)
                except ValueError as e:
                    logger.error(e.message)
                    emit.emit(e.message)
                    continue

                # 下列语句繁琐写法如 <code>if 'action' not in msg or 'uuid' not in msg:</code>
                if not all([key in msg for key in ['action', 'uuid']]):
                    continue

                self.refresh_guest_mapping()

                if msg['uuid'] not in self.guest_mapping_by_uuid:

                    if config['debug']:
                        log = u' '.join([u'uuid', msg['uuid'], u'在宿主机', self.hostname, u'中未找到.'])
                        logger.debug(log)
                        emit.debug(log)

                    continue

                self.guest = self.guest_mapping_by_uuid[msg['uuid']]
                assert isinstance(self.guest, libvirt.virDomain)

                if msg['action'] == 'reboot':
                    self.guest.reboot()
                elif msg['action'] == 'force_reboot':
                    self.guest.destroy()
                    self.guest.create()
                elif msg['action'] == 'shutdown':
                    self.guest.shutdown()
                elif msg['action'] == 'force_shutdown':
                    self.guest.destroy()
                elif msg['action'] == 'boot':
                    self.guest.create()
                elif msg['action'] == 'suspend':
                    self.guest.suspend()
                elif msg['action'] == 'resume':
                    self.guest.resume()
                elif msg['action'] == 'delete':
                    # TODO: 优化代码结构
                    self.guest.destroy()
                    self.guest.undefine()
                    root = ET.fromstring(self.guest.XMLDesc())
                    path_list = root.find('devices/disk[0]/source').attrib['name'].split('/')
                    if Guest.gf is None:
                        Guest.glusterfs_volume = path_list[0]
                        Guest.init_gfapi()

                    if Guest.gf.exists('/'.join(path_list[1:3])):
                        Guest.gf.rmtree('/'.join(path_list[1:3]))
                elif msg['action'] == 'disk-resize':
                    pass
                elif msg['action'] == 'attach-disk':
                    pass
                elif msg['action'] == 'detach-disk':
                    pass
                elif msg['action'] == 'migrate':
                    pass
                else:
                    log = u'未支持的 action：' + msg['action']
                    logger.error(log)
                    emit.emit(log)
                    pass

            except Exception as e:
                logger.error(e.message)
                emit.error(e.message)

