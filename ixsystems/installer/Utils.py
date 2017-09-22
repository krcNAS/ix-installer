from __future__ import print_function
import os, sys, re
import subprocess
import tempfile
import bsd.geom as geom
import bsd.dialog as Dialog

import freenasOS.Exceptions as Exceptions
from freenasOS.Update import PkgFileFullOnly

_avatar = None

def LoadAvatar(path="/etc/avatar.conf"):
    global _avatar
    if _avatar is None or _avatar["path"] != path:
        _avatar = {
            "AVATAR_PROJECT" : "FreeNAS",
            "path" : path,
        }
        regexp = re.compile(r'export ([^=]*)="(.*)"$')
        try:
            with open(path, "r") as conf:
                for line in conf:
                    line = line.rstrip()
                    result = regexp.match(line)
                    if result:
                        if _avatar is None:
                            _avatar = {}
                        _avatar[result.group(0)] = result.group(1)
        except:
            pass
        
class RunCommandException(RuntimeError):
    def __init__(self, code=0, command="", message="<no error>"):
        super(RunCommandException, self).__init__(message)
        self.code = code
        self.command = command
        self.message = message

    def __str__(self):
        return "Command '{}' returned {} due to '{}'".format(self.command, self.code, self.message)
    def __repr__(sefl):
        return self.__str__()

class Partition(object):
    """
    Simple wrapper for partitions.
    """
    def __init__(self, type, index, size,
                 label=None, os=False, disk=None):
        self._type = type
        self._index = index
        self._size = size
        self._label = label
        self._os = os
        self.disk = disk
        
    def __str__(self):
        return "<Partition type={}, index={}, size={}, label={}>".format(
            self.type, self.index, self.size, self.label)
    def __repr__(self):
        return "Partition({}, {}, {}, label={}, os={}, disk={})".format(
            self.type, self.index, self.size, self.label, self.os, self.disk)
    
    @property
    def disk(self):
        return self._disk
    @disk.setter
    def disk(self, d):
        self._disk = d
    @property
    def type(self):
        return self._type
    @property
    def index(self):
        return self._index
    @property
    def size(self):
        return self._size
    @property
    def os(self):
        return self._os
    @property
    def label(self):
        return self._label
    @property
    def smart_size(self):
        return SmartSize(self.size)
    
def FindMirrors(disk):
    """
    gmirror is stubborn, and we want to find any mirrors that use the given disk.
    disk is the name, e.g. ada0, not a path or partition.
    XXX: Other classes are probably just as stubborn!
    """
    mirrors = geom.class_by_name("MIRROR")
    if not mirrors:
        return
    for mirror in mirrors.geoms:
        for geom_entry in mirror.consumers:
            rn = DiskRealName(geom_entry.provider.geom)
            if rn == disk:
                yield (mirror.name, geom_entry.provider.name)
            
def SetProject(project="FreeNAS"):
    if _avatar is None:
        LoadAvatar()
    _avatar["AVATAR_PROJECT"] = project

    
def Project():
    if _avatar is None:
        LoadAvatar()
    return _avatar["AVATAR_PROJECT"]

# Convenience function
def Title():
    return Project() + " Installer"

logfile = None
def InitLog(output="/tmp/install.log"):
    global logfile
    if output.__class__ == str:
        logfile = open(output, "a")
    else:
        logfile = output
        
def LogIt(msg, exc_info=False):
    import traceback
    if logfile is None:
        InitLog()
    print(msg, file=logfile)
    if exc_info:
        exc = sys.exc_info()
        if exc:
            print("Exception {}:".format(str(exc)), file=logfile)
            for stack in traceback.extract_tb(exc[2]):
                print("\t{}".format(stack), file=logfile)
                

def BootPartitionType(diskname):
    """
    Given a disk name, determine its boot partition type.
    The boot partition type is always index 1.
    """
    LogIt("BootPartitionType({})".format(diskname))
    disk = Disk(diskname)
    if disk:
        LogIt("Found {}".format(disk))
        # Boot partition is always partition 1
        part = disk.partition(1)
        if part:
            return part.type
        else:
            LogIt("Cannot find partition 1")
    else:
        LogIt("Could not find a disk for {}".format(diskname))
    return None

def SerialConsole():
    # If the system booted via serial console, return (port, baud_rate).
    # Either value may be None.  Returns (None, None) if it can't determine
    # the values.
    try:
        uart = subprocess.check_output(["/bin/kenv", "hw.uart.console"]).rstrip()
    except:
        return (None, None)
    port_result = re.match(r'io:([0-9a-fx]+)', uart)
    if port_result:
        port = port_result.group(1)
    else:
        port = None
        
    baud_result = re.match(r'br:([0-9]+)', uart)
    if baud_result:
        br = baud_result.group(1)
    else:
        br = None
    return (port, br)

def BootMethod():
    try:
        platform = subprocess.check_output(["/bin/kenv", "grub.platform"]).rstrip()
        return platform
    except:
        return "pc"

def DiskRealName(x):
    """
    Given a geom, attempt to find out it's real name.
    (E.g., "da4p1" is "da4", "gptid/blach" is "ada18", etc.)
    """
    try:
        if x.consumer.provider.geom.consumer:
            return x.consumer.provider.geom.consumer.provider.geom.name
        else:
            return x.consumer.provider.geom.name
    except:
        return None


def SmartSize(x):
    """
    Given a size, return it as bytes, k, m, g, or t.
    This will round it down to that
    """
    size_table = [
        ["", 1024, 1],
        ["k", 1024 * 1024, 1024],
        ["m", 1024 * 1024 * 1024, 1024 * 1024],
        ["g", 1024 * 1024 * 1024 * 1024, 1024 * 1024 * 1024],
        ["t", -1, 1024 * 1024 * 1024 * 1024],
    ]
    for scale in size_table:
        if scale[1] < 0 or x < scale[1]:
            return "{}{}".format(int(x / scale[2]), scale[0])

def ParseSize(s):
    """
    The reverse of SmartSize, this returns an integer based
    on a value.
    """
    scaler = {
        'k' : 1024,
        'm' : 1024 * 1024,
        'g' : 1024 * 1024 * 1024,
        't' : 1024 * 1024 * 1024 * 1024
    }
    try:
        if s[-1] in list("kKmMgGtT"):
            suffix = s[-1].lower()
            return int(s[:-1]) * scaler[suffix]
        else:
            return int(s)
    except:
        return 0
    
def DiskInfo(name):
    """
    Return a dictionary with name, size, and description values
    """
    if name.startswith("/dev/"):
        LogIt("Tryiing geom_by_name(DEV, {})".format(name[5:]))
        name = DiskRealName(geom.geom_by_name("DEV", name[5:]))
    LogIt("Trying geom_by_name(DISK, {})".format(name))
    disk = geom.geom_by_name("DISK", name)
    if disk:
        return {
            "name" : name,
            "size" : disk.provider.mediasize,
            "description" : disk.provider.description,
            "geom" : disk,
            }
    else:
        return {}

class Disk(object):
    """
    Wrapper class for disk objects.
    Disks have a real name, size, description, and a geom object.
    They may also have partitions.
    """
    def __init__(self, iname):
        if iname.startswith("/dev/"):
            iname = iname[5:]
        name = DiskRealName(geom.geom_by_name("DEV", iname))
        if name is None:
            raise RuntimeError("Unable to find real name for disk {}".format(iname))
        disk = geom.geom_by_name("DISK", name)
        if disk:
            self._geom = disk
            self._name = name
            self._size = disk.provider.mediasize
            self._description = disk.provider.description
            part_geom = geom.geom_by_name("PART", disk.name)
            self._parts = []
            if part_geom and part_geom.providers:
                for part in part_geom.providers:
                    part_obj = Partition(type=part.config["type"],
                                         index=int(part.config["index"]),
                                         size=int(part.config["length"]),
                                         label=part.config["label"],
                                         disk=self)
                    self._parts.append(part_obj)
        else:
            raise RuntimeError("Unable to find disk {}".format(name))
    def __str__(self):
        return "<Disk {}, size={}, description={}>".format(self.name, self.size, self.description)
    def __repr__(self):
        return "Disk({})".format(self.name)
    
    @property
    def is_ssd(self):
        try:
            if int(self.geom.provider.config.get("rotationrate", 0)) == 0:
                return True
        except:
            pass
        return False
    @property
    def geom(self):
        return self._geom
    @property
    def name(self):
        return self._name
    @property
    def size(self):
        return self._size
    @property
    def smart_size(self):
        return SmartSize(self.size)
    @property
    def index(self):
        return self._index
    @property
    def description(self):
        return self._description
    @property
    def partitions(self):
        return self._parts

    def partition(self, x):
        """
        Return the partition with the given index, None otherwise
        """
        for part in self._parts:
            if part.index == x:
                return part
        return None

    def rescan(self):
        geom.scan()
        self.__init__(self._name)
        
def GetPackages(manifest, conf, cache_dir, interactive=False):
    """
    Make sure that the packages exist.  If they don't, then
    attempt to download them.  If interactive, use lots of
    dialog messages.
    """
    conf.SetPackageDir(cache_dir)
    try:
        manifest.RunValidationProgram(cache_dir, kind=Manifest.VALIDATE_INSTALL)
    except Exceptions.UpdateInvalidUpdateException as e:
        if interactive:
            Dialog.MessageBox(Title(),
                              "Invalid installation:\n\n\t" + str(e),
                              height=20, width=45).run()
        raise InstallationError(str(e))
    except BaseException as e:
        if conf.SystemManifest() is None:
            LogIt("No system manifest (duh), can't run validation program")
        else:
            LogIt("Trying to run validation program, got exception {}".format(str(e)))
            raise
    # Okay, now let's ensure all the packages are downloaded
    LogIt("Using cache directory {}".format(cache_dir))
    try:
        count = 0
        total = len(manifest.Packages())
        for pkg in manifest.Packages():
            count += 1
            LogIt("Locating package file {}-{}".format(pkg.Name(), pkg.Version()))
            if interactive:
                if os.path.exists(os.path.join(cache_dir, pkg.FileName())):
                    status = Dialog.MessageBox(Title(), "", height=8, width=60, wait=False)
                    text = "Verifying"
                else:
                    text = "Downloading and verifying"
                    status = Dialog.Gauge(Title(), "", height=8, width=60)

                status.prompt = "{} package {} ({} of {})".format(text, pkg.Name(), count, total)
                status.clear()
                status.run()
                LogIt("Started gauge")

            def DownloadHandler(path, url, size=0, progress=None, download_rate=None):
                if progress:
                    if status.__class__ == Dialog.Gauge:
                        status.percentage = progress
                LogIt("DownloadHandler({}, {}, {}, {}, {})".format(path, url, size, progress, download_rate))
            try:
                pkg_file = conf.FindPackageFile(pkg,
                                                pkg_type=PkgFileFullOnly,
                                                handler=DownloadHandler if interactive else None,
                                                save_dir=cache_dir)
            except Exceptions.ChecksumFailException as e:
                if interactive:
                    try:
                        Dialog.MessageBox(Title(),
                                          "Package {} has an invalid checksum".format(pkg.Name()),
                                          height=5, width=50).run()
                    except:
                        pass
                raise InstallationError("Invalid package checksum")
            except BaseException as e:
                LogIt("Got exception {} while trying to download package".format(str(e)))
                raise
            finally:
                if interactive:
                    if status.__class__ == Dialog.Gauge:
                        status.percentage = 100
                        dc = status.result
                
                if pkg_file is None:
                    if interactive:
                        try:
                            Dialog.MessageBox(Title(),
                                              "Unable to locate package {}".format(pkg.Name()),
                                              height=15, width=30).run()
                        except:
                            pass
                    raise InstallationError("Missing package {}".format(pkg.Name()))
                else:
                    pkg_file.close()
        try:
            # I have no idea why I need this.
            # Without this, the next YesNo dialog won't be able to use arrow keys.
            # Investigate this
            Dialog.MessageBox("", "Packages Verified", wait=False).run()
        except:
            pass

    except InstallationError:
        raise
    except BaseException as e:
        LogIt("Got exception {} while trying to load packages".format(str(e)))
        raise InstallationError(str(e))

def RunCommand(*args, **kwargs):
    # Run the given command as a sub process.
    # Either returns the output (which may be empty),
    # or raises an exception.
    error_output = tempfile.TemporaryFile()
    temp_array = [str(x) for x in args]
    command_line = " ".join(temp_array)
    chroot = kwargs.pop("chroot", None)
    
    def PreFunc():
        os.environ.pop('PYTHONPATH', None)
        os.environ['PWD'] = "/"
        os.environ['LD_LIBRARY_PATH'] = "/usr/local/lib"
        os.chroot(chroot)
        os.chdir("/")
        
    prexec_fn = None
    
    LogIt("RunCommand(\"{}\")".format(command_line))
    if chroot:
        LogIt("\tchrooted into {}".format(chroot))
        if os.geteuid() != 0:
            raise RunCommandException(code=errno.EPERM,
                                      command=command_line,
                                      message="Must be root to chroot")
    try:
        retval = ""
        retval = subprocess.check_output(temp_array,
                                         preexec_fn=PreFunc if chroot else None,
                                         stderr=error_output).decode('utf-8').rstrip()
    except subprocess.CalledProcessError as e:
        error_output.seek(0)
        error_message = error_output.read().decode('utf-8').rstrip()
        raise RunCommandException(code=e.returncode,
                                  command=command_line,
                                  message=error_message)
    finally:
        LogIt("\t{}".format(retval))
        error_output.seek(0)
        LogIt("\tStdErr: {}".format(error_output.read().decode('utf-8').rstrip()))
        error_output.close()

    return retval

def IsTruenas():
    """
    This is probably not the best name or method; what we really care about
    is whether we're going to set up the partitions a bit differently.
    """
    return _avatar.get("AVATAR_PROJECT", "FreeNAS") == "TrueNAS"

if _avatar is None:
    LoadAvatar()
    
