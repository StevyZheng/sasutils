#!/usr/bin/python
#
# Copyright (C) 2016
#      The Board of Trustees of the Leland Stanford Junior University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import print_function
import argparse
from collections import namedtuple
from itertools import ifilter
import socket
import sys

from sasutils.sas import SASHost, SASExpander, SASEndDevice
from sasutils.ses import ses_get_snic_nickname
from sasutils.sysfs import sysfs
from sasutils.vpd import vpd_decode_pg83_lu, vpd_get_page83_lu


class SASDevicesCLI(object):
    """Main class for sas_devises command-line interface."""

    def __init__(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("-v", "--verbose", action="store_true")
        self.args = parser.parse_args()

    def print_hosts(self, sysfsnode):
        sas_hosts = []
        for sas_host in sysfsnode:
            sas_hosts.append(SASHost(sas_host.node('device')))

        msgstr = "Found %d SAS hosts" % len(sas_hosts)
        if self.args.verbose:
            print("%s: %s" % (msgstr,
                              ','.join(host.name for host in sas_hosts)))
        else:
            print(msgstr)

    def print_expanders(self, sysfsnode):
        sas_expanders = []
        for expander in sysfsnode:
            sas_expanders.append(SASExpander(expander.node('device')))

        msgstr = "Found %d SAS expanders" % len(sas_expanders)
        if self.args.verbose:
            print("%s: %s" % (msgstr,
                              ','.join(exp.name for exp in sas_expanders)))
        else:
            print(msgstr)

    def _get_dev_attrs(self, sas_end_device, maxpaths=None):
        res = {}

        # Vendor info
        res['vendor'] = sas_end_device.scsi_device.attrs.vendor
        res['model'] = sas_end_device.scsi_device.attrs.model
        res['rev'] = sas_end_device.scsi_device.attrs.rev

        # Bay identifier
        res['bay'] = int(sas_end_device.sas_device.attrs.bay_identifier)

        # Size of block device
        blk_sz = sas_end_device.scsi_device.block.sizebytes()
        if blk_sz >= 1e12:
            blk_sz_info = "%.1fTB" % (blk_sz / 1e12)
        else:
            blk_sz_info = "%.1fGB" % (blk_sz / 1e9)
        res['blk_sz_info'] = blk_sz_info

        return res

    def _print_lu_devlist(self, lu, devlist, maxpaths=None):
        # use the first device for the following common attributes
        info = self._get_dev_attrs(devlist[0])
        info['lu'] = lu
        info['blkdevs'] = ','.join(dev.scsi_device.block.name
                                   for dev in devlist)
        info['sgdevs'] = ','.join(dev.scsi_device.scsi_generic.sg_devname
                                  for dev in devlist)

        # Number of paths
        paths = "%d" % len(devlist)
        if maxpaths and len(devlist) < maxpaths:
            paths += "*"
        info['paths'] = paths

        #print('%3d %10s %12s %12s %-3s %10s %10s %6s %8s' %
        #      (bay, lu, blkdevs, sgdevs, paths, vendor, model, rev,
        #       blk_sz_info))
        print('{bay:>3} {lu:>10} {blkdevs:>12} {sgdevs:>12} {paths:<3} '
              '{vendor:>10} {model:>10} {rev:>8} {blk_sz_info}'.format(**info))

    def print_end_devices(self, sysfsnode):

        devmap = {} # LU -> list of SASEndDevice

        for node in sysfsnode:
            sas_end_device = SASEndDevice(node.node('device'))

            scsi_device = sas_end_device.scsi_device
            if scsi_device.block:
                try:
                    pg83 = bytes(scsi_device.attrs.vpd_pg83)
                    lu = vpd_decode_pg83_lu(pg83)
                except AttributeError:
                    lu = vpd_get_page83_lu(scsi_device.block.name)

                devmap.setdefault(lu, []).append(sas_end_device)

        # list of set of enclosure
        encgroups = []
        orphans = []

        for lu, sas_ed_list in devmap.items():
            blklist = [d.scsi_device.block for d in sas_ed_list]
            for blk in blklist:
                if blk.array_device is None:
                    print("Warning: no enclosure set for %s in %s" %
                          (blk.name, blk.scsi_device.sysfsnode.path))
            encs = set(blk.array_device.enclosure
                       for blk in blklist
                       if blk.array_device is not None)
            if not encs:
                orphans.append((lu, sas_ed_list))
                continue
            done = False
            for encset in encgroups:
                if not encset.isdisjoint(encs):
                    encset.update(encs)
                    done = True
                    break
            if not done:
                encgroups.append(encs)

        print("Found %d enclosure groups" % len(encgroups))
        if orphans:
            print("Found %d orphan devices" % len(orphans))

        for encset in encgroups:
            encinfolist = []
            for enc in sorted(encset):
                snic = ses_get_snic_nickname(enc.scsi_generic.name)
                if snic:
                    encinfolist.append('[%s]' % snic)
                else:
                    encinfolist.append('[%s %s, addr: %s]' % (enc.attrs.vendor,
                                                              enc.attrs.model,
                                                              enc.attrs.sas_address))

            print("Enclosure group: %s" % ''.join(encinfolist))

            cnt = 0

            def enclosure_finder((lu, sas_ed_list)):
                for blk in (d.scsi_device.block for d in sas_ed_list):
                    if blk.array_device and blk.array_device.enclosure in encset:
                        return True
                return False

            encdevs = list(ifilter(enclosure_finder, devmap.items()))
            maxpaths = max(len(devs) for lu, devs in encdevs)

            if self.args.verbose:
                for lu, devlist in sorted(encdevs, key=lambda o:
                        int(o[1][0].sas_device.attrs.bay_identifier)):
                    self._print_lu_devlist(lu, devlist, maxpaths)
                    cnt += 1
            else:
                folded = {}
                for lu, devlist in encdevs:
                    devinfo = self._get_dev_attrs(devlist[0])
                    devinfo['paths'] = len(devlist)
                    del devinfo['bay'] # do not include bay on key :)
                    folded_key = namedtuple('FoldedDict', devinfo.keys())(**devinfo)
                    folded.setdefault(folded_key, []).append(devlist)
                    cnt += 1
                print("NUM   %12s %12s %6s %6s"  % ('VENDOR', 'MODEL', 'REV', 'PATHS'))
                for t, v in folded.items():
                    if maxpaths and t.paths < maxpaths:
                        pathstr = '%s*' % t.paths
                    else:
                        pathstr = '%s ' % t.paths
                    infostr = '{vendor:>12} {model:>12} {rev:>6} {paths:>6}'.format(**t._asdict())
                    print('%3d x %s' % (len(v), infostr))
            print("Total: %d block devices in enclosure group" % cnt)

        if orphans:
            print("Orphan devices:")
        for lu, blklist in orphans:
            self._print_lu_devlist(lu, blklist)


def main():
    """console_scripts entry point for sas_discover command-line."""

    sas_devices_cli = SASDevicesCLI()

    try:
        root = sysfs.node('class').node('sas_host')
        sas_devices_cli.print_hosts(root)
        root = sysfs.node('class').node('sas_expander')
        sas_devices_cli.print_expanders(root)
        root = sysfs.node('class').node('sas_end_device')
        sas_devices_cli.print_end_devices(root)
    except KeyError as err:
        print("Not found: %s" % err, file=sys.stderr)

if __name__ == '__main__':
    main()
