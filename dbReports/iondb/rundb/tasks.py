# Copyright (C) 2010 Ion Torrent Systems, Inc. All Rights Reserved

"""
Tasks
=====

The ``tasks`` module contains all the Python functions which spawn Celery
tasks in the background.

Not all functions contained in ``tasks`` are actual Celery tasks, only those
that have the  ``@task`` decorator.
"""

from __future__ import division

from celery.task import task
from celery.task import periodic_task
from celery.schedules import crontab
import urllib2
import os
import string
import random
import subprocess
import datetime
import shutil
from django.conf import settings
from django.utils import timezone
import zipfile
import os.path
import sys
import re
import json
import logging
from datetime import timedelta
import pytz

import urlparse
from ion.utils.timeout import timeout

def call(*cmd, **kwargs):
    if "stdout" not in kwargs:
        kwargs["stdout"] = subprocess.PIPE
    if "stderr" not in kwargs:
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, **kwargs)
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr


def run_as_daemon(callback, *args, **kwargs):
    """Disk And Execution MONitor (Daemon)
    Fork off a completely separate process and run callback from that process.
    """
    # fork the first time (to make a non-session-leader child process)
    try:
        pid = os.fork()
    except OSError, e:
        raise RuntimeError("1st fork failed: %s [%d]" % (e.strerror, e.errno))
    if pid != 0:
        # parent (calling) process is all done
        return
    # detach from controlling terminal (to make child a session-leader)
    os.setsid()
    try:
        pid = os.fork()
    except OSError, e:
        raise RuntimeError("2nd fork failed: %s [%d]" % (e.strerror, e.errno))
    if pid != 0:
        # child process is all done
        os._exit(0)
    # grandchild process now non-session-leader, detached from parent
    # grandchild process must now close all open files
    try:
        maxfd = os.sysconf("SC_OPEN_MAX")
    except (AttributeError, ValueError):
        maxfd = 1024

    for fd in range(maxfd):
        try:
            os.close(fd)
        except OSError: # ERROR, fd wasn't open to begin with (ignored)
            pass
    # redirect stdin, stdout and stderr to /dev/null
    os.open(os.devnull, os.O_RDWR) # standard input (0)
    os.dup2(0, 1)
    os.dup2(0, 2)
    # Run our callback function with it's arguments
    callback(*args, **kwargs)
    sys.exit()

# ZipFile doesn't provide a context manager until 2.7/3.2
if hasattr(zipfile.ZipFile, '__exit__'):
    ZipContextManager = zipfile.ZipFile
else:
    class ZipContextManager():
        def __init__(self, *args, **kwargs):
            self.zobj = zipfile.ZipFile(*args, **kwargs)
        def __enter__(self):
            return self.zobj
        def __exit__(self, type, value, traceback):
            self.zobj.close()

# Unified unzip function
def extract_zip(archive, dest, prefix=None, auto_prefix=False):
    """ unzip files in archive to destination folder
    extracting only files in prefix and omitting prefix from output path.
    """
    logger = logging.getLogger(__name__)
    # Normalize, clear out or create dest path
    dest = os.path.normpath(dest)
    if os.path.exists(dest):
        if not os.path.isdir(dest):
            raise OSError("Must extract zip file to a directory. File already exists: '%s'", dest)
        if dest.find(settings.PLUGIN_PATH) == 0:
            ## Only delete content under PLUGIN_PATH.
            delete_that_folder(dest, "Deleting content at destination path '%s'" % dest)
        else:
            raise OSError("Unable to extract ZIP - directory '%s' already exists", dest)
    os.makedirs(dest, 0777)

    logger.info("Extracting ZIP '%s' to '%s'", archive, dest)

    try:
        import pwd, grp
        uid = pwd.getpwnam('ionadmin')[2]
        gid = grp.getgrnam('ionadmin')[2]
    except OSError:
        uid = os.getuid()
        gid = os.getgid()

    extracted_files = []
    with ZipContextManager(archive, 'r') as zfobj:
        ## prefix is a string to extract from zipfile
        offset = 0
        if auto_prefix and not prefix:
            prefix, _ = get_common_prefix(zfobj.namelist())
        if prefix is not None:
            offset = len(prefix) + 1
            logger.debug("ZIP extract prefix '%s'", prefix)

        for member in zfobj.infolist():
            if member.filename[0] == '/':
                filename = member.filename[1:]
            else:
                filename = member.filename

            if prefix:
                if filename.startswith(prefix):
                    logger.debug("Extracting '%s' as '%s'", filename, filename[offset:])
                    #filename = filename[offset:]
                else:
                    logging.debug("Skipping file outside '%s' prefix: '%s'", filename, prefix)
                    continue

            targetpath = os.path.join(dest, filename)
            targetpath = os.path.normpath(targetpath)

            # Catch files we can't handle properly.
            if targetpath.find(dest) != 0:
                ## Path is no longer under dest after normalization. Prevent extraction (eg. ../../../etc/passwd)
                logging.error("ZIP archive contains file '%s' outside destination path: '%s'. Skipping.", filename, dest)
                continue

            # ZIP archives can have symlinks. Nope.
            if ((member.external_attr <<16L) & 0120000):
                logging.error("ZIP archive contains symlink: '%s'. Skipping.", member.filename)
                continue

            if "__MACOSX" in filename:
                logging.warn("ZIP archive contains __MACOSX meta folder. Skipping", member.filename)
                continue

            # Get permission set inside archive
            perm = ((member.external_attr >> 16L) & 0777 ) or 0755

            # Create all upper directories if necessary.
            upperdirs = os.path.dirname(targetpath)
            if upperdirs and not os.path.exists(upperdirs):
                logger.debug("Creating tree for '%s'", upperdirs)
                os.makedirs(upperdirs, perm | 0555)

            if filename[-1] == '/':
                # upper bits of external_attr should be 04 for folders... ignoring this for now
                if not os.path.isdir(targetpath):
                    logger.debug("ZIP extract dir: '%s'", targetpath)
                    os.mkdir(targetpath, perm | 0555)
                continue

            try:
                with os.fdopen(os.open(targetpath, os.O_CREAT|os.O_TRUNC|os.O_WRONLY, perm),'wb') as targetfh:
                    zipfh = zfobj.open(member)
                    shutil.copyfileobj(zipfh, targetfh)
                    zipfh.close()
                logger.debug("ZIP extract file: '%s' to '%s'", filename, targetpath)
            except (OSError, IOError):
                logger.exception("Failed to extract '%s':'%s' to '%s'", archive, filename, targetpath)
                continue
            # Set folder or file last modified time (ctime) to date of file in archive.
            try:
                #os.utime(targetpath, member.date_time)
                os.chown(targetpath, uid, gid)
            except (OSError, IOError) as e:
                # Non fatal if time and owner fail.
                logger.warn("Failed to set time/owner attributes on '%s': %s", targetpath , e)

            extracted_files.append(targetpath)

    return (prefix, extracted_files)

def unzipPlugin(zipfile):
    logger = logging.getLogger(__name__)
    ## Extract plugin to scratch folder. When complete, move to final location.
    plugin_path, ext = os.path.splitext(zipfile)
    plugin_name = os.path.basename(plugin_path)

    # ZIP file must named with plugin name - fragile
    # FIXME - handle (1) additions (common for multiple downloads via browser)
    # FIXME - handle version string in ZIP archive name

    scratch_path = os.path.join(settings.PLUGIN_PATH,"scratch","install-temp",plugin_name)
    (prefix, files) = extract_zip(zipfile, scratch_path, auto_prefix=True)
    if prefix:
        plugin_name = os.path.basename(prefix)

    plugin_temp_home = os.path.join(scratch_path, prefix)
    try:
        # Convert script into PluginClass, get info by introspection
        from iondb.plugins.manager import pluginmanager
        script, islaunch = pluginmanager.find_pluginscript(plugin_temp_home, plugin_name)
        logger.debug("Got script: %s", script)
        from ion.plugin.loader import cache
        ret = cache.load_module(plugin_name, script)
        cls = cache.get_plugin(plugin_name)
        p = cls()
        final_name = p.name # what the plugin calls itself, regardless of ZIP file name
        logger.info("Plugin calls itself: '%s'", final_name)
    except:
        logger.exception("Unable to interrogate plugin name from: '%s'", zipfile)
        final_name = plugin_name

    #move to the plugin dir
    # New extract_zip removes prefix from extracted files.
    # But still writes to file_name
    try:
        final_install_dir =  os.path.join(settings.PLUGIN_PATH, final_name)
        if os.path.exists(final_install_dir) and (final_install_dir != settings.PLUGIN_PATH):
            logger.info("Deleting old copy of plugin at '%s'", final_install_dir)
            delete_that_folder(final_install_dir, "Error Deleting old copy of plugin at '%s'" % final_install_dir)
        parent_folder = os.path.dirname(final_install_dir)
        if not os.path.exists(parent_folder):
            logger.info("Creating path for plugin '%s' for '%s'", parent_folder, final_install_dir)
            os.makedirs(parent_folder, 0555)

        logger.info("Moving plugin from temp extract folder '%s' to final location: '%s'", plugin_temp_home, final_install_dir)
        shutil.move(plugin_temp_home, final_install_dir)
        delete_that_folder(scratch_path, "Deleting plugin install scratch folder")
    except (IOError, OSError):
        logger.exception("Failed to move plugin from temp extract folder '%s' to final location: '%s'", plugin_temp_home, final_install_dir)
        raise

    # Now that it has been downloaded,
    # convert pre-plugin into real db plugin object
    try:
        from iondb.plugins.manager import pluginmanager
        (new_plugin, updated) = pluginmanager.install(final_name, final_install_dir)
    except ValueError:
        logger.exception("Failed to install plugin")
        #delete_that_folder(final_install_dir)

    return {
        "plugin": final_name,
        "path": final_install_dir,
        "files": files,
    }

def get_common_prefix(files):
    """For a list of files, a common path prefix and a list file names with
    the prefix removed.

    Returns a tuple (prefix, relative_files):
        prefix: Longest common path to all files in the input. If input is a
                single file, contains full file directory.  Empty string is
                returned of there's no common prefix.
        relative_files: String containing the relative paths of files, skipping
                        the common prefix.
    """
    # Handle empty input
    if not files or not any(files):
        return '', []
    # find the common prefix in the directory names.
    directories = [os.path.dirname(f) for f in files if '__MACOSX' not in f]
    prefix = os.path.commonprefix(directories)
    start = len(prefix)
    if all(f[start] == "/" for f in files):
        start += 1
    relative_files = [f[start:] for f in files]
    return prefix, relative_files


def make_relative_directories(root, files):
    directories = ( os.path.dirname(f) for f in files )
    for directory in directories:
        path = os.path.join(root, directory)
        if not os.path.exists(path):
            os.makedirs(path)

@task
def delete_that_folder(directory, message):
    logger = delete_that_folder.get_logger()
    def delete_error(func, path, info):
        logger.error("Failed to delete %s: %s", path, message)
    logger.info("Deleting %s", directory)
    shutil.rmtree(directory, onerror=delete_error)

#N.B. Run as celery task because celery runs with root permissions
@task
def removeDirContents(folder_path):
    logger = removeDirContents.get_logger()
    for file_object in os.listdir(folder_path):
        file_object_path = os.path.join(folder_path, file_object)
        if os.path.isfile(file_object_path):
            os.unlink(file_object_path)
        elif os.path.islink(file_object_path):
            os.unlink(file_object_path)
        else:
            shutil.rmtree(file_object_path)

def downloadChunks(url):
    """Helper to download large files"""

    baseFile = os.path.basename(url)
    uuid_path = ''.join([random.choice(string.letters + string.digits) for i in range(10)])

    #move the file to a more uniq path
    os.umask(0002)
    temp_path = settings.TEMP_PATH
    temp_path_uniq = os.path.join(temp_path,uuid_path)
    os.mkdir(temp_path_uniq)

    try:
        file = os.path.join(temp_path_uniq,baseFile)

        req = urllib2.urlopen(url)
        try:
            total_size = int(req.info().getheader('Content-Length').strip())
        except:
            total_size = 0
        downloaded = 0
        CHUNK = 256 * 10240
        with open(file, 'wb') as fp:
            shutil.copyfileobj(req, fp, CHUNK)
        url = req.geturl()
    except urllib2.HTTPError, e:
        logger.error("HTTP Error: %d '%s'",e.code , url)
        delete_that_folder(temp_path_uniq, "after download error")
        return False
    except urllib2.URLError, e:
        logger.error("URL Error: %s '%s'",e.reason , url)
        delete_that_folder(temp_path_uniq, "after download error")
        return False
    except:
        logger.exception("Other error downloading from '%s'", url)
        delete_that_folder(temp_path_uniq, "after download error")
        return False

    return file, url

@task
def downloadGenome(url, genomeID):
    """download a genome, and update the genome model"""
    downloadChunks(url)


import zeroinstallHelper

# Helper for downloadPlugin task
def downloadPluginZeroInstall(url, plugin):
    """ To be called for zeroinstall xml feed urls.
        Returns plugin prototype, not full plugin model object.
    """
    try:
        downloaded  = zeroinstallHelper.downloadZeroFeed(url)
        feedName = zeroinstallHelper.getFeedName(url)
    except:
        plugin.status["installStatus"] = "failed"
        plugin.status["result"] = str(sys.exc_info()[1][0])
        return False

    # The url field stores the zeroinstall feed url
    plugin.url = url
    plugin.name = feedName.replace(" ","")

    if not downloaded:
        plugin.status["installStatus"] = "failed"
        plugin.status["result"] = "processed"
        return False

    plugin.status["installStatus"] = "installed"

    # Find plugin in subdirectory of extracted and installed path
    for d in os.listdir(downloaded):
        # Skip MACOSX attribute zip artifact
        if d == '__MACOSX':
            continue
        nestedpath = os.path.join(downloaded, d)
        if not os.path.isdir(nestedpath):
            continue
        # only take subdirectory with launch.sh script
        if os.path.exists(os.path.join(nestedpath, 'launch.sh')):
            plugin.path = os.path.normpath(nestedpath)
            break
        if os.path.exists(os.path.join(nestedpath, plugin.name + '.py')):
            plugin.path = os.path.normpath(nestedpath)
            break
    else:
        # Plugin expanded without top level folder
        plugin.path = downloaded
        # assert launch.sh exists?

    plugin.status["result"] = "0install"
    # Other fields we can get from zeroinstall feed?

    # Version is parsed during install - from launch.sh, ignoring feed value
    return plugin

# Helper for downloadPlugin task
def downloadPluginArchive(url, plugin):
    ret = downloadChunks(url)
    if not ret:
        plugin.status["installStatus"] = "failed"
        plugin.status["result"] = "failed to download '%s'" % url
        return False
    downloaded, url = ret

    pdata = unzipPlugin(downloaded)

    plugin.name = pdata['plugin'] or os.path.splitext(os.path.basename(url))[0]
    plugin.path = pdata['path'] or os.path.join(settings.PLUGIN_PATH, plugin.name )

    #clean up archive file and temp dir (archive should be only file in dir)
    os.unlink(downloaded)
    os.rmdir(os.path.dirname(downloaded))

    if unzipStatus:
        plugin.status["result"] = "unzipped"
    else:
        plugin.status["result"] = "failed to unzip"

    plugin.status["installStatus"] = "installed"

    return True

@task
def downloadPlugin(url, plugin=None, zipFile=None):
    """download a plugin, extract and install it"""
    if not plugin:
        from iondb.rundb import models
        plugin = models.Plugin.objects.create(name='Unknown', version='Unknown', status={})
    plugin.status["installStatus"] = "downloading"

    logger = downloadPlugin.get_logger()

    #normalise the URL
    url = urlparse.urlsplit(url).geturl()

    if not zipFile:
        if url.endswith(".xml"):
            status = downloadPluginZeroInstall(url, plugin)
            logger.error("xml") # logfile
        else:
            status = downloadPluginArchive(url, plugin)
            logger.error("zip") # logfile

        if not status:
            # FIXME - Errors!
            installStatus = plugin.status.get('installStatus', 'Unknown')
            result = plugin.status.get('result', 'unknown')
            msg = "Plugin install '%s', Result: '%s'" % (installStatus, result)

            logger.error(msg) # logfile
            from iondb.rundb import models
            models.Message.error(msg) # UI message
            return False
    else:
        # Extract zipfile
        scratch_path = os.path.join(settings.PLUGIN_PATH,"scratch")
        zip_file = os.path.join(scratch_path, zipFile)
        plugin.status["installStatus"] = "extracting zip"

        try:
            ret = unzipPlugin(zip_file)
        finally:
            #remove the zip file
            os.unlink(zip_file)

        plugin.name = ret['plugin']
        plugin.path = ret['path']
        plugin.status["installStatus"] = "installing from zip"

    # Now that it has been downloaded,
    # convert pre-plugin into real db plugin object
    try:
        from iondb.plugins.manager import pluginmanager
        (new_plugin, updated) = pluginmanager.install(plugin.name, plugin.path)
    except ValueError:
        logger.exception("Plugin rejected by installer. Check syntax and content.")
        return None

    # Copy over download status messages and url
    new_plugin.status = plugin.status
    if plugin.url:
        new_plugin.url = plugin.url
    new_plugin.save()

    logger.info("Successfully downloaded and installed plugin %s v%s from '%s'", new_plugin.name, new_plugin.version, url)

    return new_plugin

@task
def contact_info_flyaway():
    """This invokes an external on the path which performs 3 important steps:
        Pull contact information from the DB
        Black magic
        Axeda has the contact information
    """
    logger = contact_info_flyaway.get_logger()
    logger.info("The user updated their contact information.")
    cmd = ["/opt/ion/RSM/updateContactInfo.py"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if stderr:
        logger.warning("updateContactInfo.py output error information:\n%s" % stderr)
    return stdout


@task
def static_ip(address, subnet, gateway):
    """Usage: TSstaticip [options]
         --ip      Define host IP address
         --nm      Define subnet mask (netmask)
         --nw      Define network ID
         --bc      Define broadcast IP address
         --gw      Define gateway/router IP address
    """
    logger = static_ip.get_logger()
    cmd = ["/usr/sbin/TSstaticip",
           "--ip", address,
           "--nm", subnet,
           "--gw", gateway,
           ]
    logger.info("Network: Setting host static, '%s'" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if stderr:
        logger.warning("Network error: %s" % stderr)
    return stdout


@task
def dhcp():
    """Usage: TSstaticip [options]
        --remove  Sets up dhcp, removing any static IP settings
    """
    logger = dhcp.get_logger()
    cmd = ["/usr/sbin/TSstaticip", "--remove"]
    logger.info("Network: Setting host DHCP, '%s'" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if stderr:
        logger.warning("Network error: %s" % stderr)
    return stdout


@task
def proxyconf(address, port, username, password):
    """Usage: TSsetproxy [options]
         --address     Proxy address (http://proxy.net)
         --port         Proxy port number
         --username    Username for authentication
         --password    Password for authentication
         --remove      Removes proxy setting
    """
    logger = proxyconf.get_logger()
    cmd = ["/usr/sbin/TSsetproxy",
           "--address", address,
           "--port", port,
           "--username", username,
           "--password", password,
           ]
    logger.info("Network: Setting proxy settings, '%s'" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if stderr:
        logger.warning("Network error: %s" % stderr)
    return stdout


@task
def ax_proxy():
    """Usage: TSsetproxy [options]
         --remove      Removes proxy setting
    """
    logger = ax_proxy.get_logger()
    cmd = ["/usr/sbin/TSsetproxy", "--remove"]
    logger.info("Network: Removing proxy settings, '%s'" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if stderr:
        logger.warning("Network error: %s" % stderr)
    return stdout


@task
def dnsconf(dns):
    """Usage: TSsetproxy [options]
         --remove      Removes proxy setting
    """
    logger = ax_proxy.get_logger()
    cmd = ["/usr/sbin/TSdns", "--dns", dns]
    logger.info("Network: Changing DNS settings, '%s'" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if stderr:
        logger.warning("Network error: %s" % stderr)
    return stdout


@task
def updateOneTouch():
    sys.path.append("/opt/ion/onetouch")
    from onetouch import findHosts

    #find onetouches
    if os.path.exists("/tmp/OTlock"):
        #remove the OTstatus file if it exists
        if os.path.exists("/tmp/OTstatus"):
            os.unlink("/tmp/OTstatus")
        #touch the status file
        otStatus = open("/tmp/OTstatus",'w').close()
        #run the onetouch update script
        try:
            updateStatus = findHosts.findOneTouches()
        except:
            updateStatus = "FAILED"
        otStatus = open("/tmp/OTstatus",'w')
        otStatus.write(str(updateStatus) + "\n")
        otStatus.write( "DONE\n")
        otStatus.close()
        #now remove the lock
        os.unlink("/tmp/OTlock")
        return True

    return False


@task
def build_tmap_index(reference, read_sample_size=None):
    """ Provides a way to kick off the tmap index generation
        this should spawn a process that calls the build_genome_index.pl script
        it may take up to 3 hours.
        The django server should contacts this method from a view function
        When the index creation processes has exited, cleanly or other wise
        a callback will post to a url that will update the record for the library data
        letting the genome manager know that this now exists
        until then this genome will be listed in a unfinished state.
    """

    logger = build_tmap_index.get_logger()
    fasta = os.path.join(reference.reference_path , reference.short_name + ".fasta")
    logger.debug("TMAP %s rebuild, for reference %s(%d) using fasta %s"%
         (settings.TMAP_VERSION, reference.short_name, reference.pk, fasta))

    cmd = [
        '/usr/local/bin/build_genome_index.pl',
        "--auto-fix",
        "--fasta", fasta,
        "--genome-name-short", reference.short_name,
        "--genome-name-long", reference.name,
        "--genome-version", reference.version
    ]
    if read_sample_size is not None:
        cmd.append("--read-sample-size")
        cmd.append(read_sample_size)

    ret, stdout, stderr = call(*cmd, cwd=settings.TMAP_DIR)
    if ret == 0:
        logger.debug("Successfully built the TMAP %s index for %s" %
                    (settings.TMAP_VERSION, reference.short_name))
        reference.status = 'created'
        reference.enabled = True
        reference.index_version = settings.TMAP_VERSION
        reference.reference_path = os.path.join(settings.TMAP_DIR, reference.short_name)
    else:
        logger.error('TMAP index rebuild "%s" failed:\n%s' %
                     (" ".join(cmd), stderr))
        reference.status = 'error'
        reference.verbose_error = json.dumps((stdout, stderr, ret))
    reference.save()

    return ret == 0


def IonReporterWorkflows(autorun=True):

    try:
        from iondb.rundb import models
        if autorun:
            IonReporterUploader= models.Plugin.objects.get(name="IonReporterUploader_V1_0",selected=True,active=True,autorun=True)
        else:
            IonReporterUploader= models.Plugin.objects.get(name="IonReporterUploader_V1_0",selected=True,active=True)

        logging.error(IonReporterUploader.config)
        config = IonReporterUploader.config
    except models.Plugin.DoesNotExist:
        error = "IonReporterUploader V1.0 Plugin Not Found."
        logging.error(error)
        return False, error

    try:
        headers = {"Authorization" : config["token"] }
        url = config["protocol"] + "://" + config["server"] + ":" + config["port"] +"/grws/analysis/wflist"
        logging.info(url)
    except KeyError:
        error = "IonReporterUploader V1.0 Plugin Config is missing needed data."
        logging.exception(error)
        return False, error

    try:
        #using urllib2 right now because it does NOT verify SSL certs
        req = urllib2.Request(url = url, headers = headers)
        response = urllib2.urlopen(req)
        content = response.read()
        content = json.loads(content)
        workflows = content["workflows"]
        return True, workflows
    except urllib2.HTTPError, e:
        error = "IonReporterUploader V1.0 could not contact the server."
        content = e.read()
        logging.error("Error: %s\n%s", error, content)
        return False, error
    except:
        error = "IonReporterUploader V1.0 could not contact the server."
        logging.exception(error)
        return False, error

def IonReporterVersion(plugin):
    """
    This is a temp thing for 3.0. We need a way for IRU to get the versions
    this will do that for us.
    """

    #if version is pased in use that plugin name instead
    if not plugin:
        plugin = "IonReporterUploader"

    try:
        from iondb.rundb import models
        IonReporterUploader= models.Plugin.objects.get(name=plugin,selected=True,active=True)
        logging.error(IonReporterUploader.config)
        config = IonReporterUploader.config
    except models.Plugin.DoesNotExist:
        error = plugin + " Plugin Not Found."
        logging.exception(error)
        return False, error

    try:
        headers = {"Authorization" : config["token"] }
        url = config["protocol"] + "://" + config["server"] + ":" + config["port"] + "/grws_1_2/data/versionList"
        logging.info(url)
    except KeyError:
        error = plugin + " Plugin Config is missing needed data."
        logging.debug(plugin +" config: " + config)
        logging.exception(error)
        return False, error

    try:
        #using urllib2 right now because it does NOT verify SSL certs
        req = urllib2.Request(url = url, headers = headers)
        response = urllib2.urlopen(req)
        content = response.read()
        content = json.loads(content)
        versions = content["Version List"]
        return True, versions
    except urllib2.HTTPError, e:
        error = plugin + " could not contact the server. No versions will be returned"
        content = e.read()
        logging.error("Error: %s\n%s", error, content)
    except:
        error = plugin + " could not contact the server. No versions will be returned"
        logging.exception(error)
        return False, error

@periodic_task(run_every=timedelta(days=1))
def scheduled_update_check():
    logger = scheduled_update_check.get_logger()
    try:
        check_updates.delay()
    except Exception as err:
        logger.error("TSconfig raised '%s' during a scheduled update check." % err)
        from iondb.rundb import models
        models.GlobalConfig.objects.update(ts_update_status="Update failure")
        raise
    
@periodic_task(run_every=crontab(hour="5", minute="4", day_of_week="*"))
def autoAction_report():
    # This implementation fixes the lack of logging to reportsLog.log by sending messages to
    # ionArchive daemon to do the actions (which implements the logger)
    from iondb.rundb import models
    import traceback
    import xmlrpclib
    logger = autoAction_report.get_logger()
    logger.info("Checking for Auto-action Report Data Management")

    try:
        bk = models.dm_reports.get()
    except:
        logger.error("dm_reports configuration object does not exist in database")
        raise 
        
    if bk.autoPrune:
        logger.info("Auto-action enabled")
        proxy = xmlrpclib.ServerProxy('http://127.0.0.1:%d' % settings.IARCHIVE_PORT, allow_none=True)
        #TODO: would be good to be able to filter this list somehow
        retList = models.Results.objects.all()
        for ret in retList:
            date1 = ret.timeStamp
            if timezone.is_naive(date1):
                date1 = date1.replace(tzinfo = pytz.utc)
            date2 = datetime.datetime.now(pytz.UTC)
            try:
                #Fix for TS-4983
                if ret.reportStatus == "Archived":
                    # Report has been archived so ignore any actions
                    logger.info("%s has been previously archived.  Skipping." % ret.resultsName)
                    continue
                
                timesUp = True if (date2 - date1) > timedelta(days=bk.autoAge) else False
                # N.B. the word "auto-action" in the comment is required in order to disable useless comments in the Report Log
                if not ret.autoExempt and timesUp:
                    if bk.autoType == 'P':
                        comment = '%s Pruned via auto-action' % ret.resultsName
                        proxy.prune_report(ret.pk, comment)
                        logger.debug(comment)
                    elif bk.autoType == 'A':
                        comment = '%s Archived via auto-action' % ret.resultsName
                        proxy.archive_report(ret.pk, comment)
                        logger.debug(comment)
                    elif bk.autoType == 'E':
                        comment = '%s Exported via auto-action' % ret.resultsName
                        proxy.export_report(ret.pk, comment)
                        logger.debug(comment)
                    #elif bk.autoType == 'D':
                    #    comment = '%s Deleted via auto-action' % ret.resultsName
                    #    proxy.delete_report(ret.pk, comment)
                    #    logger.info(comment)
                else:
                    if ret.autoExempt:
                        logger.info("%s is marked Exempt" % ret.resultsName)
                    else:
                        logger.info("%s is not marked Exempt" % ret.resultsName)
                        
                    if timesUp:
                        logger.info("%s exceeds time threshold" % ret.resultsName)
                    else:
                        logger.info("%s has not reached time threshold" % ret.resultsName)
            except:
                logger.exception(traceback.format_exc())
        logger.info("Auto-action complete")
    else:
        logger.info("Auto-action disabled")
        
@task
def check_updates():
    """Currently this is passed a TSConfig object; however, there might be a
    smoother design for this control flow.
    """
    logger = check_updates.get_logger()
    try:
        import ion_tsconfig.TSconfig
        tsconfig = ion_tsconfig.TSconfig.TSconfig()
        packages = tsconfig.TSpoll_pkgs()
    except Exception as err:
        logger.error("TSConfig raised '%s' during update check." % err)
        from iondb.rundb import models
        models.GlobalConfig.objects.update(ts_update_status="Update failure")
        raise
    async = None
    if packages and tsconfig.get_autodownloadflag():
        async = download_updates.delay()
        logger.debug("Auto starting download of %d packages in task %s" %
                     (len(packages), async.task_id))
    return packages, async


@task
def download_updates(auto_install=False):
    logger = download_updates.get_logger()
    try:
        import ion_tsconfig.TSconfig
        tsconfig = ion_tsconfig.TSconfig.TSconfig()
        downloaded = tsconfig.TSexec_download()
    except Exception as err:
        logger.error("TSConfig raised '%s' during a download" % err)
        from iondb.rundb import models
        models.GlobalConfig.objects.update(ts_update_status="Download failure")
        raise
    async = None
    if downloaded and auto_install:
        async = install_updates.delay()
        logger.debug("Auto starting install of %d packages in task %s" %
                     (len(downloaded), async.task_id))
    else:
        logger.debug("Finished downloading %d packages" % len(downloaded))
    return downloaded, async


def _do_the_install():
    """This function is expected to be run from a daemonized process"""
    from iondb.rundb import models
    try:
        import ion_tsconfig.TSconfig
        tsconfig = ion_tsconfig.TSconfig.TSconfig()
        success = tsconfig.TSexec_update()
        if success:
            tsconfig.set_state('F')
            models.Message.success("Upgrade completed successfully!")
        else:
            tsconfig.set_state('IF')
            models.Message.error("Upgrade failed during installation.")
    except Exception as err:
        models.GlobalConfig.objects.update(ts_update_status="Install failure")
        raise
    finally:
        # This will start celeryd if it is not running for any reason after
        # attempting installation.
        call('service', 'celeryd', 'start')


@task
def install_updates():
    logging.shutdown()
    logger = install_updates.get_logger()
    try:
        run_as_daemon(_do_the_install)
    except Exception as err:
        logger.error("The daemonization of the TSconfig installer failed: %s" % err)
        from iondb.rundb import models
        models.GlobalConfig.objects.update(ts_update_status="Install failure")
        raise

# Times out after 60 seconds
@timeout(60,None)
def free_percent(path):
    '''Returns percent of disk space that is free'''
    resDir = os.statvfs(path)
    totalSpace = resDir.f_blocks
    freeSpace = resDir.f_bavail
    if not (totalSpace > 0):
        logging.error("Path: %s : Zero TotalSpace? %d / %d", path, freeSpace, totalSpace)
        return 0
    return 100-(float(freeSpace)/float(totalSpace)*100)

# Expires after 5 minutes; is scheduled every 10 minutes
@periodic_task(run_every=timedelta(minutes=10),expires=300)
def check_disk_space():
    '''For every FileServer object, get percentage of used disk space'''
    logger = check_disk_space.get_logger()
    from iondb.rundb import models
    try:
        fileservers = models.FileServer.objects.all()
        #logger.info("Num fileservers: %d" % len(fileservers))
    except:
        logger.error(traceback.print_exc())
        return

    for fs in fileservers:
        #logger.info("Checking '%s'" % fs.filesPrefix)
        #prod automounter
        if os.path.exists(fs.filesPrefix):
            try:
                fs.percentfull = free_percent(fs.filesPrefix)
            except:
                logger.exception("Failed to compute free_percent")
                fs.percentfull = None
                
            if fs.percentfull is not None:
                fs.save()
                logger.debug("Used space: %s %0.2f%%" % (fs.filesPrefix,fs.percentfull))
            else:
                logger.warning ("could not determine size of %s" % fs.filesPrefix)
        else:
            logger.warning("directory does not exist on filesystem: %s" % fs.filesPrefix)

@task
def setRunDiskspace(experimentpk):
    '''Sets diskusage field in Experiment record with data returned from du command'''
    try:
        from iondb.rundb import models
        from django.core.exceptions import ObjectDoesNotExist
        # Get Experiment record
        exp = models.Experiment.objects.get(pk=experimentpk)
    except ObjectDoesNotExist:
        pass
    except:
        raise
    else:
        # Get filesystem location of given Experiment record
        directory = exp.expDir
        
        if not os.path.isdir(directory):
            used = 0
        else:
            # Get disk space used by the contents of the given directory in megabytes
            du = subprocess.Popen(['du', '-sm', directory], stdout=subprocess.PIPE)
            output = du.communicate()[0]
            used = output.split()[0]
        
        # Update database entry for the given Experiment record
        try:
            used = used if used != None else "0"
            exp.diskusage = int(used)
            exp.save()
        except:
            # field does not exist, cannot update
            pass
    
    
@task
def setResultDiskspace(resultpk):
    '''Sets diskusage field in Results record with data returned from du command'''
    log = setResultDiskspace.get_logger()
    try:
        from iondb.rundb import models
        from django.core.exceptions import ObjectDoesNotExist
        # Get Results record
        result = models.Results.objects.get(pk=resultpk)
        
        # Get filesystem location of given Results record
        directory = result.get_report_path()
        
        # Get disk space used by the contents of the given directory in megabytes
        du = subprocess.Popen(['du', '-sm', directory], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = du.communicate()
        if du.returncode == 0:
            output = stdout[0]
            log.info("%s -- %s" %(directory,output))
            used = output.split()[0]
        else:
            log.warning(stderr)
        
        # Update database entry for the given Experiment record
        try:
            used = used if used != None else "0"
            result.diskusage = int(used)
            result.save()
        except:
            # field does not exist, cannot update
            pass
    except ObjectDoesNotExist:
        pass
    except:
        raise

@task
def backfill_exp_diskusage():
    '''
    For every Experiment object in database, scan filesystem and determine disk usage.
    Intended to be run at package installation to populate existing databases.
    '''
    from django.db.models import Q
    from iondb.rundb import models
    
    # Setup log file logging
    filename = '/var/log/ion/%s.log' % 'backfill_exp_diskusage'
    log = logging.getLogger('backfill_diskusage')
    log.propagate = False
    log.setLevel(logging.DEBUG)
    handler = logging.handlers.RotatingFileHandler(
        filename, maxBytes=1024 * 1024 * 10, backupCount=5)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    log.addHandler(handler)
    
    log.info("")
    log.info("===== New Run =====")
            
    log.info("EXPERIMENTS:") 
    query = Q(diskusage=None) | Q(diskusage=0)
    experiment_list = models.Experiment.objects.filter(query)
    for experiment in experiment_list:
        log.info("%s" % experiment.expName)
        setRunDiskspace.delay(experiment.pk)
