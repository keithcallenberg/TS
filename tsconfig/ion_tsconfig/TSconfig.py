#!/usr/bin/env python
# Copyright (C) 2012 Ion Torrent Systems, Inc. All Rights Reserved

import os
import sys
import json
import traceback
import subprocess
import logging
import logging.handlers


class PickleLogger(object):
    """Gentle reader,
    In Celery, we're passing around a TSconfig object on which we call methods
    in order to perform the different stages of an upgrade, and because of this,
    that object is getting pickled and because of that picking the logger's
    file object needs to be delt with, and rather than manually removing it
    every time we pickle a TSconfig object and reinstantiating it every time it
    is unpickled, I wrote this ... Proxy for the logger which implements some
    of pickles serializer methods.
    Sincerely,
    Brian Kennedy
     """

    def __init__(self):
        self._logger = self.setup_logging()

    def __getstate__(self):
        obj_dict = self.__dict__.copy()
        obj_dict['_logger'] = None
        return obj_dict

    def __setstate__(self, obj_dict):
        self.__dict__.update(obj_dict)
        self._logger = self.setup_logging(False)

    def __getattr__(self, item):
        return getattr(self._logger, item)

    def __setattr__(self, key, value):
        if key == "_logger":
            self.__dict__["_logger"] = value
        else:
            setattr(self._logger, key, value)

    def setup_logging(self, rollover=True):
        """Configure the logger and return it.  This is a function because we want
        to use the logger in celery tasks as well
        """
        logger = logging.getLogger("tsconfig")
        logfile = "/var/log/ion/tsconfig_gui.log"
        if not logger.handlers:
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            hand = logging.handlers.RotatingFileHandler(logfile, backupCount=5)
            logger.addHandler(hand)
            format = logging.Formatter("%(asctime)s\t%(levelname)s\t%(funcName)s\t%(message)s")
            hand.setFormatter(format)
            # we handle the rollover manually in order to ensure that the records of a
            # single upgrade reside in a single log file.
        if rollover and os.stat(logfile).st_size > 1024 * 1024:
            hand.doRollover()
        return logger

logger = PickleLogger()

################################################################################
#
# States
#
################################################################################
#1:"Polling Failed"
#2:"No Updates Available"
#3:"Updates Available"
#4:"Package File Download Incomplete"
#5:"Package File Download Complete"
#6:"Package File Download Failed"
#7:"Package Install Incomplete"
#8:"Package Install Failed"
#9:"Package Install Complete" # Should be same as No Updates Available


################################################################################
#
# Utility functions
#
################################################################################

# TODO: this is available in ts_params
CONF_FILE="/etc/torrentserver/tsconf.conf"

def host_is_master():
    '''Returns true if current host is configured as master node'''
    if os.path.isfile(CONF_FILE):
        try:
            for line in open(CONF_FILE):
                if "mode:master" in line:
                    logger.debug("Found mode:master in %s" % CONF_FILE)
                    return True
                elif "mode:compute" in line:
                    logger.debug("Found mode:compute in %s" % CONF_FILE)
                    return False
                else:
                    pass
        except IOError as err:
            logger.error(err.message)
            
    if os.path.isfile("/opt/ion/.masternode") and not os.path.isfile("/opt/ion/.computenode"):
        logger.debug("Using flag files to determine masterhost status")
        return True
        
    raise OSError("Host not configured as either master or compute node")
    
def is_proton_ts():
    '''Checks whether configuration hardware is for PGM or Proton
    This system command requires root privilege
    This function needs to be manually synchronized with ts_function->is_proton_ts
    '''
    cmd = ["dpkg -l ion-protonupdates"]
    p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,shell=True)
    stdout, stderr = p1.communicate()
    if p1.returncode == 0:
        for line in stdout.split("\n"):
            if 'ion-protonupdates' in line:
                if line.startswith('ii'):
                    logger.info("This is a Proton TS")
                    return True
                else:
                    logger.info("This is not a Proton TS")
                    return False
    else:
        logger.error(stderr)
        return False
    
    
# This tells apt-get not to expect access to standard in.
os.environ['DEBIAN_FRONTEND'] = 'noninteractive'


################################################################################
#
#Ion database access
#Only head nodes will have dbase access
#
################################################################################
sys.path.append('/opt/ion/')
if host_is_master():
    os.environ['DJANGO_SETTINGS_MODULE'] = 'iondb.settings'
    from iondb.rundb import models

################################################################################
#
# python-apt
#
################################################################################
import apt
import apt_pkg
class GetAcquireProgress(apt.progress.base.AcquireProgress):
    '''
    Handle the package download process for apt_pkg.Acquire
    '''
    def __init__(self, tsconfig):
        apt.progress.base.AcquireProgress.__init__(self)
        self._width = 80
        self._id = 1
        self.tsconfig = tsconfig
        pass

    def start(self):
        self.tsconfig.logger.debug("[GetAcquireProgress] StartAcquire")
 
    def stop(self):
        self.tsconfig.logger.debug("[GetAcquireProgress] StopAcquire")

    def pulse(self, acquire):
        
        tsc = self.tsconfig
        tsc.pkgprogress = "%s %s/%s" % (tsc.upst[tsc.state],
                                        apt.SizeToStr(self.current_bytes), apt.SizeToStr(self.total_bytes),
                                        )
        tsc.update_progress(tsc.pkgprogress)
        
        item_idx = self.current_items
        if item_idx == self.total_items:
            item_idx -= 1
        destfile = acquire.items[item_idx].destfile
        destfile = destfile.split('/')[-1]
        debug_string = "[GetAcquireProgress] %s; CPS: %s/s; Bytes: %s/%s; Item: %s/%s" % (
            destfile,
            apt.SizeToStr(self.current_cps), 
            apt.SizeToStr(self.current_bytes), apt.SizeToStr(self.total_bytes), 
            item_idx+1, self.total_items
        )
        tsc.logger.debug(debug_string)
                  
        return True

class GetInstallProgress(apt.progress.base.InstallProgress):
    '''
    Handle the package install process for apt.Cache
    '''
    def __init__(self, tsconfig):
        apt.progress.base.InstallProgress.__init__(self)
        self.tsconfig = tsconfig
        pass

    def startUpdate(self):
        self.tsconfig.logger.debug("[GetInstallProgress] StartInstall")

    def finishUpdate(self):
        self.tsconfig.logger.debug("[GetInstallProgress] FinishInstall")

    def status_change(self, pkg, percent, status):
        tsc = self.tsconfig
        
        tsc.pkgprogress = "%s %d%%" % (status, percent)
        tsc.update_progress(tsc.pkgprogress)
                
        self.tsconfig.logger.debug("[GetInstallProgress] %s [%s/100]" % (status, percent))

################################################################################
#
# Class Definition: TSconfig
#
################################################################################
class TSconfig (object):
    
    def __init__(self):
                
        # Lists of ubuntu packages required for Torrent Server.
        # See function updatePackageLists()
        self.SYS_PKG_LIST=[]
        self.SYS_PKG_LIST_MASTER_ONLY=[]
        self.ION_PKG_LIST=[]
        self.ION_PKG_LIST_MASTER_ONLY=[]
        
        # Internal states
        self.upst = {
            'U':'Unknown',
            'C':'Checking for update',
            'A':'Available',
            'N':'No updates',
            'UF':'Update failure',
            'DL':'Downloading',
            'DF':'Download failure',
            'RI':'Ready to install',
            'I':'Installing',
            'IF':'Install failure',
            'F':'Finished installing',
        }
    
        # User-facing status messages
        self.user_status_msgs = {
            '':'Updates available',
            '':'System is up to date',
            '':'Error during update check',
            '':'Updates downloading',
            '':'Updates installing',
            '':'Error downloading',
            '':'Error installing',
            '':'Error configuring',
        }
        
        self.state = 'U'                    # Internal state of the object
        self.autodownloadenabled = False    # Auto-download flag
        self.userackdownload = False        # User acknowledged Download
        self.userackinstall = False         # User acknowledged Install
        self.testrun = False                # Flag for debugging without making any changes to system
        self.status_for_user = None         # Status string entry in database
        self.pkgprogress = None             # String of format "3/12" where package number of total package number progress.
        self.dbaccess = False               # Set when we can talk to database
        self.pkglist = []                   # List of packages with an update available
        self.logger = logger
        self.packageListFile = os.path.join('/','usr','share','ion-tsconfig','torrentsuite-packagelist.json')
        self.updatePackageLists()
                
        self.apt_cache = None        
        
        if not host_is_master():
            self.dbaccess = False
            self.logger.info("Dbase access disabled; not a head node")
        else:
            try:
                gc = models.GlobalConfig.objects.all()[0]
                self.dbaccess = True
                self.logger.info("Dbase access enabled")
            except:
                self.dbaccess = False
                self.logger.info("Dbase access disabled")
        
        self.logger.info("TSconfig.__init__() executing")
            
    #--- End of __init__ ---#

    def reload_logger(self):
        logging.shutdown()
        self.logger = PickleLogger()

    def set_testrun(self,flag):
        self.testrun = flag
        
    def get_state(self):
        return self.state
    
    def get_state_msg(self):
        return self.upst.get(self.state,'Developer Error')
        
    def set_state(self, new_state):
        if new_state == self.state:
            return
        try:
            models.GlobalConfig.objects.update(ts_update_status=self.upst[new_state])
            self.state = new_state
        except Exception as err:
            self.logger.error("Failed setting GlobalConfig ts_update_status to '%s'" % new_state)
            self.logger.exception(traceback.format_exc())
            raise err

    def reset_pkgprogress(self, current=0, total=0):
        self.progress_current = current
        self.progress_total = total
        self.pkgprogress = "%s %d/%d" % (self.upst[self.state], self.progress_current, self.progress_total)

    def update_progress(self, status):
        if self.dbaccess:
            try:
                models.GlobalConfig.objects.update(ts_update_status=status)                
            except:
                self.logger.exception("Unable to update database with progress")

    def add_pkgprogress(self, progress=1):
        self.progress_current += progress
        self.pkgprogress = "%s %d/%d" % (self.upst[self.state], self.progress_current, self.progress_total)
        self.logger.debug("Progress %s" % self.pkgprogress)
        self.update_progress(self.pkgprogress)
                
    def get_pkgprogress(self,current,total):
        return self.pkgprogress
            
    def set_autodownloadflag(self,flag):
        self.autodownloadenabled = flag
            
    def get_autodownloadflag(self):
        return self.autodownloadenabled
            
    def set_userackdownload(self,flag):
        self.userackdownload = flag
            
    def get_userackdownload(self):
        return self.userackdownload
            
    def set_userackinstall(self,flag):
        self.userackinstall = flag
            
    def get_userackinstall(self):
        return self.userackinstall
            
    def get_syspkglist(self):
        if host_is_master():
            list = self.SYS_PKG_LIST_MASTER_ONLY + self.SYS_PKG_LIST
        else:
            list = self.SYS_PKG_LIST
        return list

    def get_ionpkglist(self):
        if host_is_master():
            list = self.ION_PKG_LIST + self.ION_PKG_LIST_MASTER_ONLY
        else:
            list = self.ION_PKG_LIST
        return list
    
    ################################################################################
    #
    # Update internal list of packages to install
    #
    ################################################################################
    def updatePackageLists(self):

        try:
            
            self.logger.info("parsing %s" % self.packageListFile)
            with open(self.packageListFile,'r') as fp:
                pkgObj = json.load(fp)
        
            self.SYS_PKG_LIST               = pkgObj['packages']['system']['allservers']
            
            self.SYS_PKG_LIST_MASTER_ONLY   = pkgObj['packages']['system']['master']
            
            self.ION_PKG_LIST               = pkgObj['packages']['torrentsuite']['allservers']
            
            self.ION_PKG_LIST_MASTER_ONLY   = pkgObj['packages']['torrentsuite']['master']
            
            # ion-protonupdates is installed if T620 is determined, OR ion-pgmupdates is installed by default
            if is_proton_ts():
                self.ION_PKG_LIST_MASTER_ONLY += pkgObj['packages']['torrentsuite']['proton']
                logger.debug("Adding %s to master ion package list", ','.join(pkgObj['packages']['torrentsuite']['proton']))
            else:
                self.ION_PKG_LIST_MASTER_ONLY += pkgObj['packages']['torrentsuite']['pgm']
                logger.debug("Adding %s to master ion package list", ','.join(pkgObj['packages']['torrentsuite']['pgm']))
                            
        except:
            self.logger.exception(traceback.format_exc())
            raise
        
    ################################################################################
    #
    # Update apt repository database
    #
    ################################################################################
    def updatePkgDatabase(self):
        '''Update apt cache '''        
        try:
            self.apt_cache = apt.Cache()
            self.apt_cache.update()
            self.apt_cache.open(None)                            
            self.logger.debug("Successfully updated apt cache")
            return True
        except:
            self.logger.debug("Unable to retrieve apt cache")
            return False
    ################################################################################
    #
    # Check which packages need to be installed/upgraded
    #
    ################################################################################
    def buildPkgList(self,pkgnames):
        pkglist = []        
        apt_cache = self.apt_cache
        
        # check for virtual packages
        virtual_pkgs = []
        for pkg_name in pkgnames:
            if not apt_cache.has_key(pkg_name):
                if apt_cache.is_virtual_package(pkg_name):
                    virtual_pkgs = [pkg.name for pkg in apt_cache.get_providing_packages(pkg_name)]
                else:
                    self.logger.warn("package %s is not in apt cache" % pkg_name)
        pkgnames = [name for name in pkgnames if apt_cache.has_key(name)]
        pkgnames.extend(virtual_pkgs)
        
        # count how many packages are upgradable or new
        numpkgs = len(pkgnames)
        count = [0, 0]        
        for pkg_name in pkgnames:        
            pkg = apt_cache[pkg_name]            
            if pkg.isUpgradable:                
                pkglist.append(pkg_name)
                count[0] += 1
                self.logger.debug("version %s available for %s" % (pkg.candidateVersion, pkg.name) )
            elif not pkg.isInstalled:                
                pkglist.append(pkg_name)
                count[1] += 1
                self.logger.debug("%s not found, will install version %s" % (pkg.name, pkg.candidateVersion) )
            #else:
            #    self.logger.debug("%s does not require an update" % pkg.name)            

        self.logger.debug("Checked %s packages, found %s upgradable and %s new" % (numpkgs, count[0],count[1]))
            
        return pkglist
        
    
    ################################################################################
    #
    # Finds out if there are updates to packages
    #
    ################################################################################
    def TSpoll_pkgs(self):
        '''Returns True when there are updates to the Ion Torrent Suite software packages
        Returns list of packages to update'''
        self.set_state('C')
        if not self.updatePkgDatabase():
            self.logger.warn ("Could not update apt package database")
            self.set_state('UF')
            return None
        else:
            self.updatePackageLists()
            status, ionpkglist = self.pollForUpdates()
            if status and len(ionpkglist) > 0:
                self.logger.info("There are %d updates!" % len(ionpkglist))
                self.set_state('A')
                self.pkglist = ionpkglist
                # check available disk space
                available = self.freespace('/var')
                syspkglist = self.buildPkgList(self.get_syspkglist())
                required = self.required_download_space(ionpkglist+syspkglist)
                self.logger.debug("%.1fMB required download space, %.1fMB available in /var." % (required, available))              
                if available < required:
                    msg = "WARNING: insufficient disk space for update"                
                    self.update_progress(msg)
                    self.logger.debug(msg)                
            else:
                self.set_state('N')
            
            return list
    
    ################################################################################
    #
    # Purge package files
    #
    ################################################################################
    def TSpurge_pkgs(self):
        try:
            cmd = ['/usr/bin/apt-get',
                   'autoclean']
            p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout,stderr = p1.communicate()
            if p1.returncode == 0:
                self.logger.info ("autocleaned apt cache directory")
            else:
                self.logger.info ("Error during autoclean: %s" % stderr)
        except:
            self.logger.exception(traceback.format_exc())
    
    ################################################################################
    #
    # Download package files
    #
    ################################################################################
    def TSdownload_pkgs(self,pkglist):
        '''Uses python-apt to download available packages'''
        results = {}
        self.set_state('DL')
        apt_cache = self.apt_cache
        apt_cache.open(None) 
                
        pm = apt_pkg.PackageManager(apt_cache._depcache)
        fetcher = apt_pkg.Acquire(GetAcquireProgress(self))
        
        for pkg_name in pkglist:
            # mark packages for upgrade/install
            pkg = apt_cache[pkg_name]
            if pkg.isUpgradable:
                pkg.markUpgrade() 
            elif not pkg.isInstalled:
                pkg.markInstall()

        try:
            apt_cache._fetch_archives(fetcher, pm)
            return True
        except:
            self.logger.error(traceback.format_exc())
            return False

    ################################################################################
    #
    # Install package files
    #
    ################################################################################
    def TSinstall_pkgs(self,pkglist):
        '''Users python-apt to install packages in list'''
        self.logger.debug("Inside TSinstall_pkgs")
        numpkgs = len(pkglist)
       
        apt_cache = self.apt_cache
        apt_cache.open(None) 
                
        for i,pkg_name in enumerate(pkglist):
            # mark packages for upgrade/install
            pkg = apt_cache[pkg_name]
            if pkg.isUpgradable:
                pkg.markUpgrade() 
            elif not pkg.isInstalled:
                pkg.markInstall()        
        
            if self.testrun:
                self.add_pkgprogress()
                self.logger.info("FAKE! Installing %d of %d %s" % (i+1,numpkgs,pkg_name))
        
        if self.testrun:
            return True
            
        try:
            apt_cache.commit(GetAcquireProgress(self),GetInstallProgress(self))
            return True
        except:
            self.logger.error(traceback.format_exc())
            # Potentially do something with `failed` here, such as return it.
            return False

    ################################################################################
    #
    # Search for specific files that have an updated version available
    #
    ################################################################################
    def pollForUpdates(self):
        '''Checks for any updates for packages in ION_PKG_LIST and ION_PKG_LIST_MASTER_ONLY'''
        self.set_state('C')
        status = False
        pkglist = []        
        pkgnames = self.ION_PKG_LIST
        pkgnames.extend(self.ION_PKG_LIST_MASTER_ONLY)
        
        pkglist = self.buildPkgList(pkgnames)                
        
        if len(pkglist) > 0:
            status = True

        return status, pkglist
    
    ################################################################################
    #
    # Check required download space
    #
    ################################################################################
    def required_download_space(self, pkglist):        
        apt_cache = self.apt_cache
        apt_cache.open(None) 
        for pkg_name in pkglist:
            # mark packages for upgrade/install
            pkg = apt_cache[pkg_name]
            if pkg.isUpgradable:
                pkg.markUpgrade() 
            elif not pkg.isInstalled:
                pkg.markInstall()
        
        return apt_cache.required_download / (1024 * 1024)
    
    ################################################################################
    #
    # Available disk space in the /var partition
    #
    ################################################################################
    def freespace(self, directory):
        '''Returns free disk space for given directory in megabytes'''
        try:
            s = os.statvfs(directory)
        except:
            self.logger.exception(traceback.format_exc())
            mbytes = -1
        else:
            mbytes = (s.f_bsize * s.f_bavail) / (1024 * 1024)
            
        return mbytes


    ################################################################################
    #
    # Download sytem and Ion debian package files
    #
    ################################################################################
    def TSexec_download(self):
        self.updatePackageLists()
        self.updatePkgDatabase() 
        syspkglist = self.buildPkgList(self.get_syspkglist())
        ionpkglist = self.buildPkgList(self.get_ionpkglist())
        self.reset_pkgprogress(total=len(syspkglist) + len(ionpkglist))

        #================================
        # autoclean the apt cache
        #================================
        self.TSpurge_pkgs()
        
        #================================
        # Download system packages
        #================================
        self.logger.debug("Download system packages")
        sys_status = self.TSdownload_pkgs(syspkglist)
        if not sys_status:
            self.logger.error("Problem downloading system packages!")
            self.set_state('DF')

        #================================
        # Download Ion packages
        #================================
        self.logger.debug("Download ion packages")
        ion_status = self.TSdownload_pkgs(ionpkglist)      
        if not ion_status:
            self.logger.error("Problem downloading ion packages!")
            models.Message.error("Ion packages failed to download!", 'updates')
            self.set_state('DF')

        if sys_status and ion_status:
            self.set_state('RI')
        return syspkglist + ionpkglist
    
    ################################################################################
    #
    # Wrappers to TSconfig configuration functions
    #
    ################################################################################
    def TSpreinst_syspkg(self):
        cmd = ["/usr/sbin/TSwrapper"
               " preinst_system_packages"]
        
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,shell=True)
        stdout, stderr = p1.communicate()
        self.logger.debug("preinst_system_packages")
        self.logger.debug(stdout)
        if p1.returncode == 0:
            pass
        else:
            self.logger.error(stderr)
        
        
        return
    def TSpostinst_syspkg(self):
        cmd = ["/usr/sbin/TSwrapper"
               " config_system_packages"]
        
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,shell=True)
        stdout, stderr = p1.communicate()
        self.logger.debug("config_system_packages")
        self.logger.debug(stdout)
        if p1.returncode == 0:
            pass
        else:
            self.logger.error(stderr)
        
        return
    def TSpreinst_ionpkg(self):
        #================================
        #Nothing to do at the moment
        #================================
        self.logger.debug("TSpreinst_ionpkg")
        pass

        return
    def TSpostinst_ionpkg(self):
        cmd = ["/usr/sbin/TSwrapper"
               " config_ion_packages"]
        
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,shell=True)
        stdout, stderr = p1.communicate()
        self.logger.debug("config_ion_packages")
        self.logger.debug(stdout)
        if p1.returncode == 0:
            pass
        else:
            self.logger.error(stderr)
        
        return
    
    ################################################################################
    #
    # Install sytem and Ion debian package files and run config commands
    #
    ################################################################################
    def TSexec_update(self):
        
        try:
            self.set_state('I')
            self.logger.debug("Inside TSexec_update")
            self.logger.debug("%d and %d" % (len(self.get_syspkglist()),len(self.get_ionpkglist())))
           
            #================================
            # Get latest package lists
            #================================
            self.updatePackageLists()
            self.updatePkgDatabase() 
            syspkglist = self.buildPkgList(self.get_syspkglist())
            ionpkglist = self.buildPkgList(self.get_ionpkglist())
            
            #================================
            # Install TSconfig first so that it's code, executed through TSwrapper
            # is upgraded Before execution below.
            #================================
            self.reset_pkgprogress(total=1)
            tsconfig_result = self.TSinstall_pkgs(["ion-tsconfig"])
            if not tsconfig_result:
                self.logger.warning("Could not install ion-tsconfig!")
                return False
            
            # update the package list after tsconfig is installed
            self.updatePackageLists()
            syspkglist = self.buildPkgList(self.get_syspkglist())
            ionpkglist = self.buildPkgList(self.get_ionpkglist())

            ionpkglist = [pkg for pkg in ionpkglist if not pkg == "ion-tsconfig"]
            self.reset_pkgprogress(total=len(syspkglist) + len(ionpkglist))
    
            #================================
            # Execute pre-System package install
            #================================
            self.TSpreinst_syspkg()
            
            #================================
            # Install System packages
            #================================
            sys_result = self.TSinstall_pkgs(syspkglist)
            
            #================================
            # Execute System configuration
            #================================
            self.TSpostinst_syspkg()
            
            #================================
            # Execute pre-Ion package install
            #================================
            self.TSpreinst_ionpkg()
            
            #================================
            # Install Ion packages
            #================================    
            ion_result = self.TSinstall_pkgs(ionpkglist)
            
            #================================
            # Execute Ion configuration
            #================================
            self.TSpostinst_ionpkg()
            
        except:
            self.logger.exception(traceback.format_exc())
            
        success = sys_result and ion_result
        if success:
            self.logger.info("Successfully TSconfigured !")
        else:
            self.logger.error("Failed to TSconfigure.")
        return success

################################################################################
#
# End Class Definition: TSconfig
#
################################################################################
