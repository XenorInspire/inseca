# This file is part of INSECA.
#
#    Copyright (C) 2020-2022 INSECA authors
#
#    INSECA is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    INSECA is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with INSECA.  If not, see <https://www.gnu.org/licenses/>

import uuid
import json
import gc
import os
import shutil
import tempfile
import datetime
import tarfile
import pwd
import syslog
import Device
import base64
import sqlite3
import requests
import enum
import Utils as util
import Filesystem as filesystem
import CryptoPass as cpass
import CryptoX509 as x509
import CryptoGen as cgen
import FingerprintChunks as fpchunks
import FingerprintHash as fphash
import Configurations as confs

# IDs of the partitions used by INSECA
partid_dummy="dummy"
partid_efi="EFI"
partid_live="live"
partid_internal="internal"
partid_data="data"

def _get_run_dir():
    """Creates a run directory and returns it"""
    rundir="/run/INSECA"
    os.makedirs(rundir, exist_ok=True)
    return rundir

def _dummy_ignore(root, relative):
    """Ensure that the encrypted internal password file is not too big"""
    #util.print_event("RELATIVE_du: %s"%relative)
    if relative=="resources/internal-pass.enc":
        if os.path.getsize("%s/%s"%(root, relative))<500:
            #util.print_event("IGNORED")
            return True
    elif relative=="resources/blob0.json":
        if os.path.getsize("%s/%s"%(root, relative))<10000:
            #util.print_event("IGNORED")
            return True
    return False

def _efi_ignore(root, relative):
    """Ensure the contents of the Grub params file"""
    #util.print_event("RELATIVE_efi: %s"%relative)
    if relative=="boot/grub/bootparams.cfg":
        # contents must be equal to either bootparams0.cfg or bootparams1.cfg
        contents=util.load_file_contents("%s/%s"%(root, relative))
        f0="%s/bootparams0.cfg"%root
        f1="%s/bootparams1.cfg"%root
        if not os.path.exists(f0) or not os.path.exists(f1):
            return cgen.generate_password() # force the process to fail
        c0=util.load_file_contents(f0)
        c1=util.load_file_contents(f1)
        if contents!=c0 and contents!=c1:
            return cgen.generate_password() # force the process to fail
        #util.print_event("IGNORED")
        return True
    return False

#
# Misc.
#
def deactivate_gdm_autologin():
    """Make sure GDM's autologin is turned off (if GDM is present)"""
    gdmconf_file="/etc/gdm3/daemon.conf"
    if not os.path.exists(gdmconf_file):
        return

    econf=util.load_file_contents(gdmconf_file)
    nconf=[]
    for line in econf.splitlines():
        parts=line.split("=")
        if len(parts)==2 and parts[0] in ("AutomaticLoginEnable", "TimedLoginEnable"):
            nconf+=["%s=false"%parts[0]]
        else:
            nconf+=[line]
    nconf="\n".join(nconf)
    util.write_data_to_file(nconf, gdmconf_file)
    (status, out, err)=util.exec_sync(["systemctl", "reload", "gdm"])
    if status==0:
        syslog.syslog(syslog.LOG_INFO, "Deactivated GDM autologin")
    else:
        syslog.syslog(syslog.LOG_WARNING, "Could not deactivate GDM autologin: %s"%err)

def compute_integrity_fingerprint(dev, blob1_priv, live_hash):
    """Computes the "integrity fingerprint" of the device, using the @live_hash*
    fingerprints which already must have been computed/verified.
    Returns (presec password, integrity log)"""
    if not isinstance(dev, Device.Device):
        raise Exception("CODEBUG: @dev is not a Device object")
    if not isinstance(blob1_priv, str):
        raise Exception("CODEBUG: invalid @blob1_priv: expected an str")

    # inter partitions data
    util.print_event("Hashing inter partitions data")
    (interhash, log)=dev.compute_inter_partitions_hash()

    # add blob1's private key
    import hashlib
    sha256=hashlib.sha256()
    sha256.update(blob1_priv.encode())
    hash=fphash.chain_integity_hash(interhash, blob1_priv)
    log+=[{"blob1": hash[:5]}]

    # partitions table data
    util.print_event("Hashing MBR/GPT data")
    layout=dev.get_partitions_layout()
    ftype=util.LabelType(layout["type"])
    newhash=fphash.compute_partitions_table_hash(dev.devfile, ftype)
    hash=fphash.chain_integity_hash(hash, newhash)
    log+=[{"mbr": hash[:5]}]

    # "dummy" partition's contents
    util.print_event("Hashing 'dummy' partition data")
    newhash=fphash.compute_directory_hash(dev.mount(partid_dummy), _dummy_ignore)
    hash=fphash.chain_integity_hash(hash, newhash)
    log+=[{"dummy": hash[:5]}]

    # "EFI" partition's contents
    util.print_event("Hashing 'EFI' partition data")
    newhash=fphash.compute_directory_hash(dev.mount(partid_efi), _efi_ignore)
    hash=fphash.chain_integity_hash(hash, newhash)
    log+=[{"efi-data": hash[:5]}]

    # FIXME: add "internal" and "data" canaries's data

    # "live" partition's contents
    hash=fphash.chain_integity_hash(hash, live_hash)
    log+=[{"live": hash[:5]}]
    return (hash, log)

def install_live_linux_files_from_iso(live_path, source_dir):
    """Installs a new version of the Live Linux system (kernel + initrd + squash filesystem) in the @live_path (which must have been previously mounted).
    The resources are supposed to be all in @source_dir.
    """
    os.makedirs(live_path, exist_ok=True)
    os.chmod(live_path, 0o700)
    util.print_event("Installing live Linux components")

    # make sure we have enough space to copy files
    free_b=shutil.disk_usage(live_path).free
    for fname in ["vmlinuz", "initrd.img", "filesystem.squashfs"]:
        srcfile="%s/live/%s"%(source_dir, fname)
        dstfile="%s/%s"%(live_path, fname)
        if os.path.exists(dstfile):
            # dstfile will be overwriten => space it currently occupies will be made available
            free_b+=os.stat(dstfile).st_size
        free_b-=os.stat(srcfile).st_size
    free_b-=+500*1024 # keep 500K for misc. filesystem housekeeping
    if free_b<=0:
        raise Exception("Not enough free space to extract new live Linux components (missing %s)"%-free_b)
    syslog.syslog(syslog.LOG_INFO, "Live Linux update: %s bytes will remain after files installation"%free_b)

    # remove any existing file in the new directory
    for filename in os.listdir(live_path):
        fpath="%s/%s"%(live_path, filename)
        os.remove(fpath)

    # copy live Linux files
    for fname in ["vmlinuz", "initrd.img", "filesystem.squashfs"]:
        util.print_event("Copying the '%s' component to device"%fname)
        srcfile="%s/live/%s"%(source_dir, fname)
        dstfile="%s/%s"%(live_path, fname)
        shutil.copyfile(srcfile, dstfile)

class InvalidCredentialException(Exception):
    pass

class DeviceIntegrityException(Exception):
    pass

class UnlockFailedReasonType(int, enum.Enum):
    """Device unlock failure reason types"""
    CREDENTIAL = 0
    INTEGRITY = 1
    OTHER = 2
    TOO_MANY_ATTEMPTS = 3

class UpdatesStatus(str, enum.Enum):
    """Device update status"""
    IDLE = "Idle"
    DOWNLOAD = "Downloading update information"
    CHECK = "Checking for an update"
    STAGE = "Staging update"
    APPLY = "Applying staged update"

class BootProcessWKS:
    """Class to help asserting integrity of a "workstation" device. See the Installer object to understand the operations
    performed here"""
    __instance = None

    @staticmethod
    def get_instance(live_env=None):
        """Method to get a singleton"""
        if BootProcessWKS.__instance is None and live_env:
            BootProcessWKS.__instance = BootProcessWKS(live_env)
        return BootProcessWKS.__instance

    def __init__(self, live_env, dev=None):
        if not isinstance(live_env, Environ):
            raise Exception("CODEBUG: @live_env should be an Environ object")
        self._live_env=live_env
        if dev:
            if not isinstance(dev, Device.Device):
                raise Exception("CODEBUG: @dev should be a Device object")
            self._dev=dev
        else:
            live_devpart=util.get_root_live_partition()
            self._dev=Device.Device(util.get_device_of_partition(live_devpart))

        self._user_uuid=None
        self._cn=None

    def __del__(self):
        """Call this function when done"""
        self._dev=None
        gc.collect()

    def _unlock_blob0(self, user_password):
        """Get the blob0 from the user's password, and retreives information about user.
        Returns the blob0 (as a string)"""
        # blob0 might contain something like: {"d3a96fec-d9f4-4a77-bc10-ce8f88796cd8": {"mode": "password", "salt": "I4&G...e\\m", "enc-blob": "sha256:sVTJ...T0=", "cn": "Firstname Lastname"}}
        eobj0=cpass.CryptoPassword(user_password, ignore_password_strength=True)
        mp=self._dev.mount(partid_dummy)
        blobs=json.loads(util.load_file_contents("%s/resources/blob0.json"%mp))
        for slot in blobs:
            entry=blobs[slot]
            encdata=entry["enc-blob"]
            try:
                try:
                    if "salt" in entry:
                        salt=entry["salt"]
                    else:
                        salt="not really some salt" # for INSECA created before using the password hardening with salt
                    password=cpass.harden_password_for_blob0(user_password, salt)
                    eobj=cpass.CryptoPassword(password)
                    blob0=eobj.decrypt(encdata).decode()
                except Exception:
                    blob0=eobj0.decrypt(encdata).decode()
                self._user_uuid=slot
                util.write_data_to_file(slot, "%s/user_uuid"%_get_run_dir())
                self._cn=entry["cn"]

                # change user's comment, for the UI
                if self._live_env.logged is not None:
                    util.change_user_comment(self._live_env.logged, self._cn)
                return blob0
            except Exception as e:
                pass
        self._dev.umount(partid_dummy)
        raise InvalidCredentialException("Invalid password")

    def _unblock_with_blob0(self, dummy_mountpoint, blob0):
        # Use blob0 to decrypt blob1's private key and load it
        log=None

        try:
            # decrypt blob1
            eobj=cpass.CryptoPassword(blob0)
            encdata=util.load_file_contents("%s/resources/blob1.priv.enc"%dummy_mountpoint)
            blob1=eobj.decrypt(encdata).decode()
        except Exception as e:
            raise Exception("Could not load 'blob1.priv.enc' or decrypt blob1 from blob0", None)

        try:
            # load "live" chunks
            eobj=x509.CryptoKey(blob1, None)
            echunks=util.load_file_contents("%s/resources/chunks.enc"%dummy_mountpoint)
            chunks=json.loads(eobj.decrypt(echunks))
        except Exception as e:
            raise Exception("Could not load 'chunks.enc' or decrypt chuks from blob1", None)

        try:
            lmp=self._dev.mount(partid_live)
            (hash0, log0)=fpchunks.verify_files_chunks(lmp, chunks)
        except Exception as e:
            raise Exception("Could not verify files in the live partition (%s)"%str(e), None)

        try:
            # compute integrity fingerprint
            (ifp, log)=compute_integrity_fingerprint(self._dev, blob1, hash0)
            log+=[{"live": log0}]
        except Exception as e:
            raise Exception("Could not compute the integrity fingerprint (%s)"%str(e), None)

        try:
            # load internal partition's password
            eobj=cpass.CryptoPassword(ifp)
            data=util.load_file_contents("%s/resources/internal-pass.enc"%dummy_mountpoint)
        except Exception as e:
            raise Exception(f"Could not load the 'internal-pass.enc' file: {str(e)}", log)

        try:
            # decrypt internal partition's password
            int_password=eobj.decrypt(data).decode()
            return (eobj, int_password)
        except Exception as e:
            raise Exception("Could not decrypt the 'internal-pass.enc' file from the integrity hash", log)

    def check_integrity(self, blob0):
        """Check the integrity of the device using @blob0
        Raise an exception if the device has been modified.
        """
        dmp=self._dev.mount(partid_dummy)
        self._unblock_with_blob0(dmp, blob0)

    def unlock(self, user_password):
        """Starts the whole process of "opening" the device while making all the verifications
        Returns the (blob0, int_password, data_password) tuple, to be used to apply staged updates if any
        """
        if self._live_env.unlocked:
            raise Exception("Device already unlocked")
        dmp=self._dev.mount(partid_dummy)

        # authenticate device
        try:
            verifiers={"Admin": {
                "type": "key",
                "public-key-file": "%s/resources/meta-sign.pub"%dmp
                }
            }
            self._dev.verify(verifiers)
        except Exception as e:
            self._dev.umount(partid_dummy)
            raise e

        # get to blob0 and blob1's private key
        blob0=self._unlock_blob0(user_password)

        try:
            (eobj, int_password)=self._unblock_with_blob0(dmp, blob0)

            # unlock and mount "internal" partition
            self._dev.set_partition_secret(partid_internal, "password", int_password)
            self._dev.mount(partid_internal, "/internal", options="nodev,x-gvfs-hide", auto_umount=False)

            # unlock and mount "data" partition
            data=util.load_file_contents("/internal/credentials/data-pass.enc")
            data_password=eobj.decrypt(data).decode()
            self._dev.set_partition_secret("data", "password", data_password)
            os.makedirs("/data")
            fstype=self._dev.get_partition_filesystem("data")
            options="nodev,x-gvfs-hide"
            if fstype in (filesystem.FSType.fat, filesystem.FSType.exfat):
                options+=",uid=1000,gid=1000"
            self._dev.mount(partid_data, "/data", options=options, auto_umount=False)
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "While unlocking device: %s"%str(e))
            raise DeviceIntegrityException("Device may be compromised")
        finally:
            self._dev.umount(partid_dummy)

        self._live_env.update_unlocked_status()

        # user password change to the provided password
        (status, out, err)=util.exec_sync(["chpasswd"], stdin_data="insecauser:%s"%user_password)
        if status!=0:
            raise Exception("Could not change logged user's password: %s"%err)

        # deactivate GDM autologin
        deactivate_gdm_autologin()

        try:
            # extract the PRIVDATA of all the components
            self._live_env.extract_privdata()

            # map data/ directories
            self.map_directories()

            # extract all the component's specific code
            self._live_env.extract_live_config_scripts()

            # create SSH server keys if on 1st boot (we don't want to have the same SSH keys on all the devices)
            self._live_env.configure_ssh_keys()

            # if there is a post unlock script, execute it now
            post_unlock_script="/opt/share/post-unlock-script"
            if os.path.exists(post_unlock_script):
                (status, out, err)=util.exec_sync([post_unlock_script])
                if status!=0:
                    raise Exception("Could not execute post unlock script '%s': %s"%(post_unlock_script, err))
        except Exception as e:
            self._live_env.events.add_exception_event("post-start", str(e))
            syslog.syslog(syslog.LOG_ERR, "post-start failed: %s"%str(e))
            raise e

        return (blob0, int_password, data_password)

    def map_directories(self):
        """Map directories from the /data partition"""
        map_file="/opt/share/inseca-data-map.json"
        data_map=json.load(open(map_file, "r"))
        for key in data_map:
            dest=data_map[key]
            src="/data/%s"%key
            if not os.path.exists(src):
                if os.path.exists(dest):
                    # initialize @src with @dest's contents before "replacing it" (via the bind mount)
                    shutil.copytree(dest, src)
                else:
                    raise Exception("Could not bind 'data/%s': directories don't exist"%key)
            os.makedirs(dest, exist_ok=True)
            syslog.syslog(syslog.LOG_INFO, "Binding %s to %s"%(src, dest))
            (status, out, err)=util.exec_sync(["mount", "--bind", "-o", "x-gvfs-hide", src, dest])
            if status!=0:
                raise Exception("Could not bind 'data/%s' to '%s': %s"%(src, dest, err))

    def unmap_directories(self):
        map_file="/opt/share/inseca-data-map.json"
        if not os.path.exists(map_file):
            return
        data_map=json.load(open(map_file, "r"))
        for key in data_map:
            dest=data_map[key]
            syslog.syslog(syslog.LOG_INFO, "Unbinding %s"%dest)
            (status, out, err)=util.exec_sync(["umount", dest])
            if status!=0:
                raise Exception("Could not unbind '%s': %s"%(dest, err))

    def prepare_shutdown(self):
        """Unmount partitions before shuting down"""
        try:
            self.unmap_directories()
            self._dev.umount(partid_data)
            # don't umount partid_internal as it will be busy
        except Exception as e:
            syslog.syslog(syslog.LOG_WARNING, "Error unmount partition: %s"%str(e))


#
# users settings' backup and restore parameters
#
def _backup_dconf(live_env, backup_filename):
    os.seteuid(live_env.uid)
    cenv=os.environ.copy()
    live_env.define_UI_environment()
    cenv["HOME"]=live_env.home_dir # dconf seems to use $HOME

    (status, out, err)=util.exec_sync(["dconf", "dump", "/"], exec_env=cenv)
    os.seteuid(0)

    if status!=0:
        raise Exception("Could not backup DCONF: %s"%err)

    util.write_data_to_file(out, backup_filename)

def _restore_dconf(live_env, backup_filename):
    if os.path.exists(backup_filename):
        data=util.load_file_contents(backup_filename)

        live_env.define_UI_environment()
        os.seteuid(live_env.uid)
        cenv=os.environ.copy()
        cenv["HOME"]=live_env.home_dir
        (status, out, err)=util.exec_sync(["dconf", "load", "/"], exec_env=cenv, stdin_data=data)
        os.seteuid(0)

        if status!=0:
            raise Exception("Could not restore DCONF: %s"%err)

def _backup_firefox(live_env, backup_filename):
    ffdir="%s/.mozilla/firefox"%live_env.home_dir
    if os.path.exists(ffdir):
        try:
            # get rid of any running firefox which locks SQLite databases
            util.exec_sync(["killall", "firefox-esr"])
            util.exec_sync(["killall", "firefox"])
            util.exec_sync(["killall", "firefox-bin"])
        except:
            pass
        tar=tarfile.TarFile.gzopen(backup_filename, "w")
        for entry in os.listdir(ffdir):
            if os.path.isdir("%s/%s"%(ffdir, entry)):
                # https://support.mozilla.org/en-US/kb/profiles-where-firefox-stores-user-data
                for name in ["places.sqlite", "favicons.sqlite", "xulstore.json"]:
                    targetfile="%s/%s/%s"%(ffdir, entry, name)
                    if os.path.exists(targetfile):
                        tar.add(targetfile, arcname=".mozilla/firefox/%s/%s"%(entry, name))
        tar.close()

def _backup_network(live_env, backup_filename):
    netdir="/etc/NetworkManager/system-connections"
    if os.path.exists(netdir):
        tar=tarfile.TarFile.gzopen(backup_filename, "w")
        for entry in os.listdir(netdir):
            # we don't want VPN files!
            try:
                path="%s/%s"%(netdir, entry)
                backup=True
                contents=util.load_file_contents(path)
                for line in contents.splitlines():
                    if line.startswith("type="):
                        (dummy, ctype)=line.split("=")
                        if ctype=="vpn":
                            backup=False
                        break
                if backup:
                    tar.add(path, entry)
            except:
                pass
        tar.close()

def _restore_network(live_env, backup_filename):
    netdir="/etc/NetworkManager/system-connections"
    if os.path.exists(backup_filename):
        # extract connection's files
        tar=tarfile.TarFile.open(backup_filename, "r")
        tar.extractall(path=netdir)
        tar.close()

        # get the name of the wireless interface, and the associated MAC address
        wlan=None
        hwaddr=None
        (status, out, err)=util.exec_sync(["/sbin/iw", "dev"])
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Unable to get list of Wi-Fi adapters: %s"%err)
            return

        for line in out.splitlines():
            line=line.strip()
            if line.startswith("Interface"):
                if wlan:
                    wlan=None
                    hwaddr=None
                    break # more than 1 Wi-Fi interface
                parts=line.split()
                if len(parts)!=2:
                    syslog.syslog(syslog.LOG_ERR, "Unexpected 'iw' command output: %s"%out)
                    wlan=None
                    hwaddr=None
                    break # unexpected output!
                wlan=parts[1]
            elif wlan and line.startswith("addr"):
                parts=line.split()
                if len(parts)!=2:
                    syslog.syslog(syslog.LOG_ERR, "Unexpected 'iw' command output: %s"%out)
                    wlan=None
                    hwaddr=None
                    break # unexpected output!
                hwaddr=parts[1].upper()

        # replace MAC address in each config file, if there is one, otherwise do nothing
        syslog.syslog(syslog.LOG_INFO, "hwaddr: %s"%hwaddr)
        if hwaddr:
            for filename in os.listdir(netdir):
                path="%s/%s"%(netdir, filename)

                restore=True
                contents=util.load_file_contents(path)
                for line in contents.splitlines():
                    if line.startswith("type="):
                        (dummy, ctype)=line.split("=")
                        if ctype=="vpn":
                            restore=False
                        break

                if restore:
                    newlines=[]
                    for line in contents.splitlines():
                        if line.startswith("mac-address="):
                            line="mac-address= %s"%hwaddr
                        newlines+=[line]
                    contents="\n".join(newlines)
                    util.write_data_to_file(contents, path)
                else:
                    os.remove(path)

        # force NetworkManager to reload configuration
        (status, out, err)=util.exec_sync(["nmcli", "connection", "reload"])
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not reload NetworkManager's configuration: %s"%err)
        syslog.syslog(syslog.LOG_INFO, "Reloaded NM!")

def _backup_home_dir_as_archive(live_env, backup_filename, rel_data_dir):
    fullpath="%s/%s"%(live_env.home_dir, rel_data_dir)
    if os.path.exists(fullpath):
        tar=tarfile.TarFile.gzopen(backup_filename, "w")
        tar.add(fullpath, arcname=rel_data_dir)
        tar.close()

def _restore_archive_in_home_dir(live_env, backup_filename):
    if os.path.exists(backup_filename):
        tar=tarfile.TarFile.open(backup_filename, "r")
        tar.extractall(path=live_env.home_dir)
        tar.close()

_user_config_definition={
    "ssh.tgz": {
        "rel-source-dir": ".ssh",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "gpg.tgz": {
        "rel-source-dir": ".gnupg",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "vinagre_bookmarks.tgz": {
        "rel-source-dir": ".local/share/vinagre/vinagre-bookmarks.xml",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "remmina_bookmarks.tgz": {
        "rel-source-dir": ".local/share/remmina",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "remmina_prefs.tgz": {
        "rel-source-dir": ".config/remmina/remmina.pref",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "keepassxc0.tgz": {
        "rel-source-dir": ".config/keepassxc/keepassxc.ini",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "keepassxc1.tgz": {
        "rel-source-dir": ".cache/keepassxc/keepassxc.ini",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "zed.tgz": {
        "rel-source-dir": ".primx",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "putty.tgz": {
        "rel-source-dir": ".putty",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "desktop-conf": {
        "backup-func": _backup_dconf,
        "restore-func": _restore_dconf
    },
    "monitors": {
        "rel-source-dir": ".config/monitors.xml",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "firefox-bookmarks.tgz": {
        "backup-func": _backup_firefox,
        "restore-func": _restore_archive_in_home_dir
    },
    "chromium.tgz": {
        "rel-source-dir": ".config/chromium/Default/Bookmarks",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "network.tgz": {
        "backup-func": _backup_network,
        "restore-func": _restore_network
    },
    "keyring.tgz": {
        "rel-source-dir": ".local/share/keyrings",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
    "nextcloud.tgz": {
        "rel-source-dir": ".config/Nextcloud",
        "backup-func": _backup_home_dir_as_archive,
        "restore-func": _restore_archive_in_home_dir
    },
}


class Environ:
    """Object to get information about a "workstation" or "admin" live environment"""
    def __init__(self):
        # determine live Linux type
        infos_file="/opt/share/keyinfos.json"
        try:
            infos=json.load(open(infos_file, "r"))
            self._live_type=confs.BuildType(infos["build-type"])
        except:
            raise Exception("Invalid or missing keyinfos.json file")

        self._events=None
        self._ssh_keys_dir=None
        self._default_profile_dir=None
        if self._live_type in (confs.BuildType.WKS, confs.BuildType.SERVER, confs.BuildType.ADMIN):
            self._events=Events()
            self._ssh_keys_dir="/internal/ssh"
            self._default_profile_dir="/internal/default-profile"

        self._logged=None
        self._uid=None
        self._gid=None

        self._unlocked=False
        self.update_unlocked_status()

        self.components_live_config_dir="/tmp/components-live-config"
        self.privdata_dir="/tmp/privdata"

    def update_unlocked_status(self):
        """Computes/updates the "unlocked status" of the environment: if the /internal directory is a mount point"""
        self._unlocked=False
        if self._live_type in (confs.BuildType.WKS, confs.BuildType.SERVER, confs.BuildType.ADMIN):
            (status, out, err)=util.exec_sync(["findmnt", "/internal"])
            if status==0:
                self._unlocked=True
        else:
            self._unlocked=True # basic live Linux => no startup performed

    @property
    def events(self):
        return self._events

    @property
    def unlocked(self):
        """Tells if the startup has already been done"""
        return self._unlocked

    @property
    def logged(self):
        """Logged user name"""
        if self._logged is None:
            (status, out, err)=util.exec_sync(["who"])
            # output will be like "insecauser tty2 [...]" for Wayland or "insecauser :0 [...]" for X11
            if status!=0:
                raise Exception("Can't get the name of the connected user")
            for line in out.splitlines():
                parts=line.split()
                if len(parts)>0 and (parts[1].startswith("tty") or parts[1]==":0"):
                    self._logged=parts[0]                 # logged user name
                    entry=pwd.getpwnam(self._logged)
                    self._uid=entry.pw_uid                # logged user UID
                    self._gid=entry.pw_gid                # logged user GID
                    self._home_dir=entry.pw_dir
        return self._logged

    @property
    def uid(self):
        """Logged user UID"""
        if self._logged is None:
            self.logged
        return self._uid

    @property
    def gid(self):
        """Logged user GID"""
        if self._logged is None:
            self.logged
        return self._gid

    @property
    def home_dir(self):
        """Logged user HOME dir"""
        if self._logged is None:
            self.logged
        return self._home_dir

    @property
    def config_dir(self):
        """Name of the directory where the user specific config files are kept (saved at shutdown and restored)
        upon sucessful authentication.
        It is created if it does yet exist"""
        assert self._live_type==confs.BuildType.WKS

        user_uuid=util.load_file_contents("%s/user_uuid"%_get_run_dir())
        path="/internal/user-config/%s"%user_uuid
        os.makedirs(path, exist_ok=True, mode=0o700)
        return path

    @property
    def default_profile_dir(self):
        return self._default_profile_dir

    def define_UI_environment(self):
        if self.uid is None:
            return False
        os.environ["XDG_RUNTIME_DIR"]="/run/user/%d"%self.uid
        os.environ["DBUS_SESSION_BUS_ADDRESS"]="unix:path=/run/user/%d/bus"%self.uid
        os.environ["WAYLAND_DISPLAY"]="wayland-0"
        os.environ["DISPLAY"]=":0"
        # look for the XAuthority file which can be /run/user/<uid>/gdm/Xauthority if Gnome runs over X11, or
        # /run/user/<uid>/.mutter-Xwaylandauth.* if Gnome runs over Wayland
        xauthset=False
        for fname in os.listdir(f"/run/user/{self.uid}"):
            if fname.startswith(".mutter-Xwaylandauth"):
                os.environ["XAUTHORITY"]=f"/run/user/{self.uid}/{fname}"
                xauthset=True
                break
        if not xauthset:
            os.environ["XAUTHORITY"]=f"/run/user/{self.uid}/gdm/Xauthority"
        return True

    #
    # PRIVDATA
    #
    def extract_privdata(self):
        """Decrypt and extract /privdata.tar.enc file (which contains the PRIVDATA of all the components)"""
        privtmp=self.privdata_dir
        os.makedirs(privtmp, mode=0o700, exist_ok=True)

        if self._live_type in (confs.BuildType.WKS, confs.BuildType.SERVER, confs.BuildType.ADMIN):
            privkey_file="/internal/credentials/privdata-ekey.priv"
        else:
            privkey_file="/credentials/privdata-ekey.priv"

        if os.path.exists("/privdata.tar.enc") and os.path.exists(privkey_file):
            eobj=x509.CryptoKey(util.load_file_contents(privkey_file), None)
            edata=util.load_file_contents("/privdata.tar.enc")
            resfile=eobj.decrypt(edata, return_tmpobj=True)
            (status, out, err)=util.exec_sync(["tar", "xf", resfile.name, "-C", privtmp])
            if status!=0:
                raise Exception("Error extracting privdata.tar.enc: %s"%err)
            syslog.syslog(syslog.LOG_INFO, "privdata.tar.enc extracted in %s"%privtmp)

        # extract each component's PRIVDATA file AS-IS in /
        exec_env=os.environ.copy()
        exec_env["PYTHONPATH"]=os.path.dirname(__file__)
        components=os.listdir(privtmp)
        for component in components:
            if os.path.exists("%s/%s"%(privtmp, component)):
                try:
                    syslog.syslog(syslog.LOG_INFO, "Copying PRIVDATA for component '%s' to root"%component)
                    tmptar=tempfile.NamedTemporaryFile()
                    tmptar.close()
                    tarobj=tarfile.open(tmptar.name, mode='w')
                    tarobj.add("%s/%s"%(privtmp, component), arcname=".", recursive=True)
                    tarobj.close()
                    tarobj=tarfile.open(tmptar.name, mode='r')
                    tarobj.extractall("/")
                except Exception as e:
                    syslog.syslog(syslog.LOG_ERR, "Failed to extact PRIVDATA for component '%s': %s"%(component, str(e)))

    #
    # component's config
    #
    def extract_live_config_scripts(self):
        """Decrypt and extract the source code of all the component's configure scripts"""
        if os.path.exists(self.components_live_config_dir):
            syslog.syslog(syslog.LOG_WARNING, "CODEBUG: directory '%s' should not exist"%self.components_live_config_dir)
            shutil.rmtree(self.components_live_config_dir)
        os.makedirs(self.components_live_config_dir, mode=0o700)

        if self._live_type in (confs.BuildType.WKS, confs.BuildType.SERVER, confs.BuildType.ADMIN):
            privkey_file="/internal/credentials/privdata-ekey.priv"
        else:
            privkey_file="/credentials/privdata-ekey.priv"

        if os.path.exists("/live-config.tar.enc") and os.path.exists(privkey_file):
            eobj=x509.CryptoKey(util.load_file_contents(privkey_file), None)
            edata=util.load_file_contents("/live-config.tar.enc")
            resfile=eobj.decrypt(edata, return_tmpobj=True)

            (status, out, err)=util.exec_sync(["tar", "xf", resfile.name, "-C", self.components_live_config_dir])
            if status!=0:
                raise Exception("Error extracting live config. code")
            syslog.syslog(syslog.LOG_INFO, "live-config.tar.enc extracted in %s"%self.components_live_config_dir)

    def configure_components(self, stage):
        """Configure all the components for which there is a configure<stage>.py script"""
        assert isinstance(stage, int)

        if os.path.exists(self.components_live_config_dir):
            exec_env=os.environ.copy()
            exec_env["PYTHONPATH"]=os.path.dirname(__file__)
            for component in os.listdir(self.components_live_config_dir):
                script="%s/%s/configure%s.py"%(self.components_live_config_dir, component, stage)
                # use the configure.py script, if any
                if os.path.exists(script):
                    syslog.syslog(syslog.LOG_INFO, "Initializing component '%s', stage %d"%(component, stage))
                    exec_env["PRIVDATA_DIR"]="%s/%s"%(self.privdata_dir, component)
                    if self._live_type in (confs.BuildType.WKS, confs.BuildType.SERVER):
                        exec_env["USERDATA_DIR"]="/internal/components/%s"%component
                    (status, out, err)=util.exec_sync([script], exec_env=exec_env)
                    if status!=0:
                        raise Exception("Error initializing component '%s': %s"%(component, err))

    #
    # SSH keys unique to each device
    #
    def configure_ssh_keys(self):
        os.makedirs(self._ssh_keys_dir, exist_ok=True, mode=0o700)
        ssh_privkey="%s/ssh_host_ed25519_key"%self._ssh_keys_dir
        ssh_pubkey="%s.pub"%ssh_privkey
        if not os.path.exists(ssh_privkey):
            (status, out, err)=util.exec_sync(["ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f", ssh_privkey])
            if status!=0:
                self.events.add_exception_event("ssh-privatekey-generation", err)
            (status, out, err)=util.exec_sync(["ssh-keygen", "-y", "-f", ssh_privkey])
            if status!=0:
                self.events.add_exception_event("ssh-publickey-generation", err)
                os.remove(ssh_privkey)
            util.write_data_to_file(out, ssh_pubkey)

        # remove any existing SSH server key and deploy the specific keys
        for filename in os.listdir("/etc/ssh"):
            if filename.startswith("ssh_host_"):
                os.remove("/etc/ssh/%s"%filename)
        shutil.copyfile(ssh_privkey, "/etc/ssh/ssh_host_ed25519_key")
        os.chmod("/etc/ssh/ssh_host_ed25519_key", 0o400)
        shutil.copyfile(ssh_pubkey, "/etc/ssh/ssh_host_ed25519_key.pub")
        (status, out, err)=util.exec_sync(["systemctl", "restart", "sshd"])
        if status!=0:
            if not err.endswith("Unit sshd.service not found."):
                raise Exception("Could not restart SSHD service after keys update: %s"%err)

    #
    # User setting management
    #
    def user_config_clean_nobackup(self):
        """emove any previous NO-BACKUP file presence"""
        path="%s/NO-BACKUP"%self.config_dir
        if os.path.exists(path):
            os.remove(path)

    def user_config_backup(self):
        """Back up pre-defined user settings to the directory associated to the user.
        Needs to be run as root"""
        assert self._live_type==confs.BuildType.WKS

        syslog.syslog(syslog.LOG_INFO, "Starting backing up user config")
        config_dir=self.config_dir
        path="%s/NO-BACKUP"%config_dir
        if os.path.exists(path):
            # don't back up the setting this time
            syslog.syslog(syslog.LOG_INFO, "Found the %s file, don't backup user config this time"%path)
            return

        definition=_user_config_definition
        for key in definition:
            try:
                self.events.add_info_event("backup", "Backing up %s"%key)
                syslog.syslog(syslog.LOG_INFO, "Backup: %s"%key)
                backup_filename="%s/%s"%(config_dir, key)
                conf=definition[key]
                bfunc=conf["backup-func"]
                if "rel-source-dir" in conf:
                    bfunc(self, backup_filename, conf["rel-source-dir"])
                else:
                    bfunc(self, backup_filename)
            except Exception as e:
                syslog.syslog(syslog.LOG_WARNING, "Could not backup user config '%s': %s"%(key, str(e)))
                self.events.add_exception_event("backup", e)

    def user_config_restore(self):
        """Restore any previously backed up user settings, if any"""
        assert self._live_type==confs.BuildType.WKS

        config_dir=self.config_dir
        definition=_user_config_definition
        for key in definition:
            try:
                backup_filename="%s/%s"%(config_dir, key)
                if os.path.exists(backup_filename):
                    self.events.add_info_event("backup", "Restoring %s"%key)
                    syslog.syslog(syslog.LOG_INFO, "Restore: %s"%key)
                    conf=definition[key]
                    bfunc=conf["restore-func"]
                    bfunc(self, backup_filename)
            except Exception as e:
                syslog.syslog(syslog.LOG_WARNING, "Could not restore user config '%s': %s"%(key, str(e)))
                self.events.add_exception_event("backup", e)

    def user_config_remove(self):
        """Remove any previously backed up user settings and mark the settings as not to be saved the next time"""
        assert self._live_type==confs.BuildType.WKS

        config_dir=self.config_dir
        exp=None
        for fname in os.listdir(config_dir):
            try:
                os.remove("%s/%s"%(config_dir, fname))
            except Exception as e:
                exp=e
        util.write_data_to_file("", "%s/NO-BACKUP"%config_dir)
        if exp:
            raise exp

    #
    # Desktop Environment interactions
    #
    def notify(self, message):
        """Display a notification. 
        Make sure define_UI_environment() was called"""
        args=["zenity", "--notification", "--text", message]
        if util.is_run_as_root():
            args=["sudo", "-H", "-u", self.logged, "DBUS_SESSION_BUS_ADDRESS=%s"%os.environ["DBUS_SESSION_BUS_ADDRESS"]]+args  
        util.exec_sync(args)

    def user_setting_get(self, section, what):
        """Get a user setting. 
        Make sure define_UI_environment() was called"""
        args=["gsettings", "get", section, what]
        if util.is_run_as_root():
            args=["sudo", "-H", "-u", self.logged, "DBUS_SESSION_BUS_ADDRESS=%s"%os.environ["DBUS_SESSION_BUS_ADDRESS"]]+args
        (status, out, err)=util.exec_sync(args)
        if status==0:
            return out
        else:
            raise Exception("Could not get user's setting '%s %s': %s"%(section, what, err))

    def user_setting_set(self, section, what, value):
        """Change a user setting. 
        Make sure define_UI_environment() was called"""
        args=["gsettings", "set", section, what, value]
        if util.is_run_as_root():
            args=["sudo", "-H", "-u", self.logged, "DBUS_SESSION_BUS_ADDRESS=%s"%os.environ["DBUS_SESSION_BUS_ADDRESS"]]+args
        (status, out, err)=util.exec_sync(args)
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not define setting '%s %s' to '%s': %s"%(section, what, value, err))

class Events:
    def __init__(self):
        self._db_filename="/internal/events.db"
        self._conn=None

        # define attestation file
        self._attestation_file="/internal/credentials/attestation.json"
        self.device_id=None

        self.home_base_url="https://" # FIXME

        self._backlog=[] # store events while internal dir is not mounted

    def _ensure_device_id(self):
        if not self.device_id:
            attest=open(self._attestation_file).read()
            adata=json.loads(attest)
            self.device_id=adata["attestation"]["device-id"]

    def _open_db(self):
        if self._conn:
            return True
        elif os.path.exists("/internal/resources/config.json"):
            # open SQLite connection
            self._conn=sqlite3.connect(self._db_filename, check_same_thread=False)
            self._conn.isolation_level=None
            self._init_db()
            os.chmod(self._db_filename, 0o600)
            syslog.syslog(syslog.LOG_INFO, "Connection opened to %s"%self._db_filename)

            # empty backlog
            syslog.syslog(syslog.LOG_INFO, "Backlog: %s"%self._backlog)
            if len(self._backlog)>0:
                c=self._conn.cursor()
                for (sql, params) in self._backlog:
                    c.execute(sql, params)
                self._backlog=[]
            return True
        else:
            return False

    def _init_db(self):
        c=self._conn.cursor()
        sql="""CREATE TABLE IF NOT EXISTS events (
               device_id TEXT NOT NULL,
               ts INTEGER NOT NULL,
               type TEXT NOT NULL,
               data TEXT NOT NULL)"""
        c.execute(sql)

    def declare_device(self):
        try:
            self._ensure_device_id()
            attest=open(self._attestation_file).read()
            attest=json.loads(attest)
            ssh_key=util.load_file_contents("/etc/ssh/ssh_host_ed25519_key.pub")
            data={"attestation": attest, "ssh-key": ssh_key}
            data=json.dumps(data)
            data=base64.b64encode(data.encode()).decode()
            now=datetime.datetime.utcnow()
            now=int(now.timestamp())

            sql="INSERT INTO events (device_id, ts, type, data) VALUES (:device_id, :ts, :type, :data)"
            params={"device_id": self.device_id, "ts": now, "type": "DECL", "data": data}
            if self._open_db():
                c=self._conn.cursor()
                c.execute(sql, params)
                self.send_events() # try now!
            else:
                self._backlog+=[[sql, params]]
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "Failed to declare device: %s"%str(e))

    def _get_default_data_set(self):
        self._ensure_device_id()
        (dtotal, davail)=util.get_partition_data_sizes("/data")
        (itotal, iavail)=util.get_partition_data_sizes("/internal")
        return {
            "device-id": self.device_id,
            "docs-total": dtotal,
            "docs-available": davail,
            "internal-total": itotal,
            "internal-available": iavail,
        }

    def add_booted_event(self):
        hw_descr=util.get_hw_descr()
        hw_mem=util.get_hw_mem()
        data=self._get_default_data_set()
        data["hw"]=hw_descr
        data["mem"]=hw_mem
        self._add_event("BOOT", data)

    def add_shutdown_event(self):
        self._add_event("SHUTDOWN", self._get_default_data_set())

    def add_windows_start_event(self):
        self._add_event("WINDOWS-START", self._get_default_data_set())

    def add_windows_stop_event(self):
        self._add_event("WINDOWS-STOP", self._get_default_data_set())

    def add_update_event(self, data):
        (total, avail)=util.get_partition_data_sizes("/internal")
        if not data:
            data={}
        data["live-space-total"]=total
        data["live-space-available"]=avail
        self._add_event("UPDATE", data)

    def add_info_event(self, module, message):
        self._add_event("INFO", {"module": module, "msg": str(message)})

    def add_exception_event(self, module, exception):
        self._add_event("ERROR", {"module": module, "error": str(exception)})

    def _add_event(self, event_type, event_data):
        """Add an event to the list of events. @event_data MUST be a simple key=value dictionary where values are strings or integers"""
        try:
            self._ensure_device_id()
            now=datetime.datetime.utcnow()
            now=int(now.timestamp())

            data=json.dumps(event_data, sort_keys=True)

            sql="INSERT INTO events (device_id, ts, type, data) VALUES (:device_id, :ts, :type, :data)"
            params={"device_id": self.device_id, "ts": now, "type": event_type, "data": data}
            if self._open_db():
                c=self._conn.cursor()
                c.execute(sql, params)
                self.send_events() # try now!
            else:
                self._backlog+=[[sql, params]]
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "Failed to record event '%s': %s"%(event_type, str(e)))

    def send_events(self):
        """Send all pending events home"""
        try:
            if self._open_db():
                events=[]
                c=self._conn.cursor()
                for row in c.execute("SELECT device_id, ts, type, data FROM events ORDER BY ts ASC"):
                    events+=[[row[0], row[1], row[2], row[3]]]

                for (device_id, ts, event_type, data) in events:
                    if event_type=="DECL":
                        r=requests.post("%s/%s"%(self.home_base_url, "/f3c8d053e5a3"), data=data, timeout=3)
                    else:
                        data=json.loads(data)
                        data["device-id"]=device_id
                        data["ts"]=ts
                        data["event-type"]=event_type
                        r=requests.get("%s/%s"%(self.home_base_url, "/23a71f253d31"), params=data, timeout=3)
                    if r.status_code==200:
                        c.execute("DELETE FROM events WHERE ts=:ts AND type=:type", {"ts": ts, "type": event_type})
            else:
                syslog.syslog(syslog.LOG_ERR, "Failed to send events: /internal is not mounted")
        except:
            pass


#
# Users management
#
def declare_user(dummy_mountpoint, name, user_password, blob0):
    """Declare/modify a user in the blob0.json file (which is created on first call)."""
    done=False
    path="%s/resources/blob0.json"%dummy_mountpoint
    salt=cpass.generate_salt()
    password=cpass.harden_password_for_blob0(user_password, salt)
    if os.path.exists(path):
        users=json.loads(util.load_file_contents(path))
        for user_uuid in users:
            userdata=users[user_uuid]
            if userdata["cn"]==name and userdata["mode"]=="password":
                eobj=cpass.CryptoPassword(password)
                userdata["enc-blob"]=eobj.encrypt(blob0)
                userdata["salt"]=salt
                done=True
                break
    else:
        users={}

    if not done:
        user_uuid=str(uuid.uuid4())
        eobj=cpass.CryptoPassword(password)
        users[user_uuid]={
            "mode": "password",
            "salt": salt,
            "enc-blob": eobj.encrypt(blob0),
            "cn": name
        }
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    util.write_data_to_file(json.dumps(users), path)

def get_users(dummy_mountpoint):
    """List all declared users"""
    path="%s/resources/blob0.json"%dummy_mountpoint
    if not os.path.exists(path):
        raise Exception("Device has not yet been initialized")

    users=json.loads(util.load_file_contents(path))
    res=[]
    for user_uuid in users:
        userdata=users[user_uuid]
        res +=[userdata["cn"]]
    res.sort()
    return res

def delete_user(dummy_mountpoint, name, internal_mountpoint):
    """Delete a user"""
    path="%s/resources/blob0.json"%dummy_mountpoint
    if not os.path.exists(path):
        raise Exception("Device has not yet been initialized")

    users=json.loads(util.load_file_contents(path))
    if len(users)==1:
        raise Exception("Can't remove user when there is only one")
    for user_uuid in list(users.keys()):
        userdata=users[user_uuid]
        if userdata["cn"]==name:
            # undeclare user
            del users[user_uuid]

            # remove any associated config data
            if internal_mountpoint:
                cpath="%s/user-config/%s"%(internal_mountpoint, user_uuid)
                try:
                    if os.path.exists(cpath):
                        shutil.rmtree(cpath)
                except Exception as e:
                    syslog.syslog(syslog.LOG_WARNING, "Could not remove user config data '%s': %s"%(cpath, str(e)))

    util.write_data_to_file(json.dumps(users), path)

def change_user_password(dummy_mountpoint, current_password, new_password):
    """Change the user's password, when the current password is known"""
    if current_password==new_password:
        return

    eobj0=cpass.CryptoPassword(current_password) # for INSECA created before using the password hardening

    path="%s/resources/blob0.json"%dummy_mountpoint
    if not os.path.exists(path):
        raise Exception("Device has not yet been initialized")
    users=json.loads(util.load_file_contents(path))
    for user_uuid in users:
        userdata=users[user_uuid]
        blob0=None
        try:
            if "salt" in userdata:
                salt=userdata["salt"]
            else:
                salt="not really some salt" # for INSECA created before using the password hardening with salt
            password=cpass.harden_password_for_blob0(current_password, salt)
            eobj=cpass.CryptoPassword(password)
            blob0=eobj.decrypt(userdata["enc-blob"])
        except Exception:
            try:
                blob0=eobj0.decrypt(userdata["enc-blob"])
            except Exception:
                pass
        if blob0:
            declare_user(dummy_mountpoint, userdata["cn"], new_password, blob0)
            return
    raise Exception("Invalid password")

def reset_user_password(dummy_mountpoint, name, user_password, blob0):
    """Reset (overwrite) the user's password"""
    declare_user(dummy_mountpoint, name, user_password, blob0)