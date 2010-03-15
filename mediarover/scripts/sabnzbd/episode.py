# Copyright 2009 Kieran Elliott <kierse@mediarover.tv>
#
# Media Rover is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# Media Rover is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import logging.config
import os
import re
import shutil
import sys
from optparse import OptionParser
from tempfile import TemporaryFile
from time import strftime

from mediarover import locate_config_files
from mediarover.config import read_config
from mediarover.error import *
from mediarover.scripts.error import *
from mediarover.series import Series
from mediarover.source.filesystem.episode import FilesystemEpisode, FilesystemMultiEpisode
from mediarover.utils.configobj import ConfigObj
from mediarover.utils.filesystem import series_episode_exists, series_episode_path, series_season_path, series_season_multiepisodes, clean_path
from mediarover.version import __app_version__

# public methods - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

def sort():

	""" parse command line options """

	usage = "usage: %prog [options] <result_dir> <nzb_name> <nice_name> <newzbin_id> <category> <newsgroup> <status>"
	parser = OptionParser(usage=usage, version=__app_version__)

	# location of config dir
	parser.add_option("-c", "--config", metavar="/PATH/TO/CONFIG/DIR", help="path to application configuration directory")

	# dry run
	parser.add_option("-d", "--dry-run", action="store_true", default=False, help="simulate downloading nzb's from configured sources")

	(options, args) = parser.parse_args()

	""" config setup """

	if options.config:
		config_dir = options.config
	elif os.name == "nt":
		if "LOCALAPPDATA" in os.environ: # Vista or better default path
			config_dir = os.path.expandvars("$LOCALAPPDATA\Mediarover")
		else: # XP default path
			config_dir = os.path.expandvars("$APPDATA\Mediarover")
	else: # os.name == "posix":
		config_dir = os.path.expanduser("~/.mediarover")

	# make sure application config file exists and is readable
	locate_config_files(config_dir)

	# create config object using user config values
	config = read_config(config_dir)

	""" logging setup """

	# initialize and retrieve logger for later use
	# set logging path using default_log_dir from config file
	logging.config.fileConfig(open(os.path.join(config_dir, "sabnzbd_episode_sort_logging.conf")))
	logger = logging.getLogger("mediarover.scripts.sabnzbd.episode")

	""" post configuration setup """

	# make sure script was passed 6 arguments
	if not len(args) == 7:
		print "Warning: must provide 7 arguments when invoking %s" % os.path.basename(sys.argv[0])
		parser.print_help()
		exit(1)

	# capture all logging output in local file.  If sorting script exits unexpectedly,
	# or encounters an error and gracefully exits, the log file will be placed in
	# the download directory for debugging
	tmp_file = None
	if config['logging']['generate_sorting_log']:
		tmp_file = TemporaryFile()
		handler = logging.StreamHandler(tmp_file)
		formatter = logging.Formatter('%(asctime)s %(levelname)s - %(message)s - %(filename)s:%(lineno)s')
		handler.setFormatter(formatter)
		logger.addHandler(handler)

	# sanitize tv series filter subsection names for 
	# consistent lookups
	for name, filters in config['tv']['filter'].items():
		del config['tv']['filter'][name]
		config['tv']['filter'][Series.sanitize_series_name(name, ignore_metadata=config['tv'].as_bool('ignore_series_metadata'))] = filters

	""" main """

	logger.info("--- STARTING ---")
	logger.debug("using config directory: %s", config_dir)

	# check if user has requested a dry-run
	if options.dry_run:
		logger.info("--dry-run flag detected!  Download will not be sorted during execution!")

	fatal = 0
	try:
		_process_download(config, options, args)
	except Exception, e:
		fatal = 1
		logger.exception(e)

		if config['logging']['generate_sorting_log']:

			# reset current position to start of file for reading...
			tmp_file.seek(0)

			# flush log data in temporary file handler to disk 
			sort_log = open(os.path.join(args[0], "sort.log"), "w")
			shutil.copyfileobj(tmp_file, sort_log)
			sort_log.close()

	exit(fatal)

# private methods - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

def _process_download(config, options, args):

	logger = logging.getLogger("mediarover.scripts.sabnzbd.episode")

	logger.debug(sys.argv[0] + " '%s' '%s' '%s' '%s' '%s' '%s' '%s'" % tuple(args))

	"""
	arguments:
	  1. The final directory of the job (full path)
	  2. The name of the NZB file
	  3. User modifiable job name
	  4. Newzbin report number (may be empty)
	  5. Newzbin or user-defined category
	  6. Group that the NZB was posted in e.g. alt.binaries.x
	  7. Status
	"""
	path = args[0]
	nzb = args[1]
	job = args[2]
	report_id = args[3]
	category = args[4]
	group = args[5]
	status = args[6]

	# remove any unwanted characters from the end of the download path
	path = path.rstrip("/\ ")

	tv_root = config['tv']['tv_root']

	# check to ensure we have the necessary data to proceed
	if path is None or path == "":
		raise InvalidArgument("path to completed job is missing or null")
	elif os.path.basename(path).startswith("_FAILED_"):
		logger.warning("download is marked as failed, moving to trash...")
		try:
			args[0] = _move_to_trash(tv_root[0], path)
		except OSError, (num, message):
			if num == 13:
				logger.error("unable to move download directory to '%s', permission denied", args[0])
		finally:
			raise FailedDownload("unable to sort failed download")
	elif job is None or job == "":
		raise InvalidArgument("job name is missing or null")
	elif status == 1:
		raise FailedDownload("download failed verification")
	elif status == 2:
		raise FailedDownload("download failed unpack")
	elif status == 3:
		raise FailedDownload("download failed verification and unpack")

	shows = {}
	alias_map = {}
	for root in tv_root:

		# make sure tv root directory exists and that we have read and 
		# write access to it
		if not os.path.exists(root):
			raise FilesystemError("TV root directory (%s) does not exist!", (root))
		if not os.access(root, os.R_OK | os.W_OK):
			raise FilesystemError("Missing read/write access to tv root directory (%s)", (root))

		logger.info("begin processing tv directory: %s", root)

		ignore_metadata = True
		if 'ignore_series_metadata' in config['tv']:
			ignore_metadata = config['tv'].as_bool('ignore_series_metadata')

		# set umask for files and directories created during this session
		os.umask(config['tv']['umask'])

		# get list of shows in root tv directory
		dir_list = os.listdir(root)
		dir_list.sort()
		for name in dir_list:

			# skip hidden directories
			if name.startswith("."):
				continue

			dir = os.path.join(root, name)
			if os.path.isdir(dir):

				series = Series(name, path=dir, ignore_metadata=ignore_metadata)
				sanitized_name = Series.sanitize_series_name(series)

				if sanitized_name in shows:
					logger.warning("duplicate series directory found! Multiple directories for the same series can result in sorting errors!  You've been warned...")

				# process series aliases
				if sanitized_name in config['tv']['filter']:
					if 'alias' in config['tv']['filter'][sanitized_name]:
						series.aliases = config['tv']['filter'][sanitized_name]['alias'];
						count = 0
						for alias in series.aliases:
							sanitized_alias = Series.sanitize_series_name(alias, series.ignore_metadata)
							if sanitized_alias in alias_map:
								logger.warning("duplicate series alias found! Duplicate aliases (when part of two different series filters) can/will result in incorrect downloads and improper sorting! You've been warned...")
							alias_map[sanitized_alias] = sanitized_name
							count += 1
						logger.info("%d alias(es) identified for series '%s'" % (count, series))

				shows[sanitized_name] = series
				logger.debug("watching series: %s => %s", sanitized_name, dir)

	logger.info("watching %d tv show(s)", len(shows))
	logger.debug("finished processing watched tv")

	ignored = [ext.lower() for ext in config['tv']['ignored_extensions']]

	# locate episode file in given download directory
	orig_path = None
	filename = None
	extension = None
	size = 0
	for file in os.listdir(path):
		if os.path.isfile(os.path.join(path, file)):
			
			# check if current file's extension is in list
			# of ignored extensions
			(name, ext) = os.path.splitext(file)
			ext = ext.lstrip(".")
			if ext.lower() in ignored:
				continue

			# get size of current file (in bytes)
			stat = os.stat(os.path.join(path, file))
			if stat.st_size > size:
				filename = file
				extension = ext
				size = stat.st_size
				logger.debug("identified possible download: filename => %s, size => %d", filename, size)
	else:
		if filename is None:
			raise FilesystemError("unable to find episode file in given download path '%s'", path)

		orig_path = os.path.join(path, filename)
		logger.info("found download file at '%s'", orig_path)

	# build episode object using command line values
	if report_id is not None and report_id != "":
		from mediarover.source.newzbin.episode import NewzbinEpisode, NewzbinMultiEpisode
		
		try:
			if NewzbinMultiEpisode.handle(job):
				episode = NewzbinMultiEpisode.new_from_string(job)
			else:
				episode = NewzbinEpisode.new_from_string(job)
				
		except MissingParameterError:
			logger.error("Unable to parse job name (%s) and extract necessary values", job)
			raise

	# other download
	else:
		from mediarover.source.episode import Episode, MultiEpisode

		try:
			if MultiEpisode.handle(job):
				episode = MultiEpisode.new_from_string(job)
			else:
				episode = Episode.new_from_string(job)

		except MissingParameterError:
			logger.error("Unable to parse job name (%s) and extract necessary values", job)
			raise

	# build a filesystem episode object
	try:
		episode.episodes
	except AttributeError:
		episode = FilesystemEpisode.new_from_episode(episode, filename, extension)
	else:
		episode = FilesystemMultiEpisode.new_from_episode(episode, filename, extension)

	# make sure episode series object knows whether to ignore metadata or not
	episode.series.ignore_metadata = config['tv']['ignore_series_metadata']

	# determine if episode belongs to a currently watched series
	season_path = None
	additional = None

	# get actual name of series
	sanitized_name = Series.sanitize_series_name(episode.series)
	if sanitized_name in alias_map:
		sanitized_name = alias_map[sanitized_name]

	try:
		episode.series = shows[sanitized_name]

	# if not, we need to create the series directory
	except KeyError:

		logger.info("series directory not found")
		series_dir = os.path.join(tv_root[0], episode.format_series(config['tv']['template']['series']))
		season_path = os.path.join(series_dir, episode.format_season(config['tv']['template']['season']))
		try:
			os.makedirs(season_path)
			logger.info("created series directory '%s'", series_dir)
		except OSError, (num, message):
			if num == 13:
				logger.error("unable to create series directory '%s', permission denied", season_path)
			raise
		finally:
			episode.series.path = series_dir
			shows[sanitized_name] = episode.series
	
	# series directory found, look for season directory
	else:

		# look for existing season directory
		try:
			season_path = series_season_path(episode.series, episode.season, config['tv']['ignored_extensions'])

		# if not, we need to create it
		except FilesystemError:
			dir = episode.format_season(config['tv']['template']['season'])
			season_path = os.path.join(episode.series.path, dir)
			try:
				os.mkdir(season_path)
			except OSError, (num, message):
				if num == 13:
					logger.error("unable to create season directory '%s', permission denied", season_path)
				raise

		# season directory found, perform some last minute checks before we 
		# start moving stuff around
		else:
			if series_episode_exists(episode.series, episode, config['tv']['ignored_extensions']):
				logger.warning("duplicate episode detected: %s", filename)
				additional = strftime("%Y%m%d%H%M")

	# generate new filename for current episode
	new_path = os.path.join(season_path, episode.format_episode(
		series_template = config['tv']['template']['series_episode'],
		daily_template = config['tv']['template']['daily_episode'], 
		smart_title_template = config['tv']['template']['smart_title'],
		additional = additional
	))

	# move downloaded file to new location and rename
	if not options.dry_run:
		try:
			shutil.move(orig_path, new_path)
			logger.info("moving downloaded episode '%s' to '%s'", orig_path, new_path)
		except OSError, (num, message):
			if num == 13:
				logger.error("unable to move downloaded episode to '%s', permission denied", new_path)
			raise
	
		# move successful, cleanup download directory
		else:

			# clean up download directory by removing all files matching ignored extensions list.
			# if unable to download directory (because it's not empty), move it to .trash
			try:
				clean_path(path, ignored)
				logger.info("removing download directory '%s'", path)
			except (OSError, FilesystemError):
				logger.error("unable to remove download directory '%s'", path)

				try:
					args[0] = _move_to_trash(tv_root[0], path)
					logger.info("moving download directory '%s' to '%s'", path, args[0])
				except OSError, (num, message):
					if num == 13:
						logger.error("unable to move download directory to '%s', permission denied", args[0])
					raise

			# if aggressive flag is set, check to see if current download
			# made any existing files redundant or unnecessary
			if config['tv']['multiepisode']['aggressive']:
				logger.info("aggressive flag set to True, checking for redundant files...")
	
				list = []
				try:
					episode.episodes

				# single episode
				except AttributeError:
					if not config['tv']['multiepisode']['prefer']:
						for multi in series_season_multiepisodes(episode.series, episode.season, config['tv']['ignored_extensions']):
							if episode in multi.episodes:
								for ep in multi.episodes:
									# ATTENTION: when a file is moved using shutil, its modification time IS NOT updated
									# this means that the cache won't be regenerated (because its stale) meaning we won't 
									# find the current episode.  Therefore, skip the current episode as we know its in the 
									# correct place
									if ep != episode and not series_episode_exists(ep.series, ep, config['tv']['ignored_extensions']):
										break
								else:
									logger.info("scheduling '%s' for deletion", multi)
									list.append(multi)

				# multipart episode
				else:
					if config['tv']['multiepisode']['prefer']:
						for ep in episode.episodes:
							if series_episode_exists(ep.series, ep, config['tv']['ignored_extensions']):
								logger.info("scheduling '%s' for deletion", ep)
								list.append(file)

				# iterate over list of stale downloads and remove them from filesystem
				finally:
					for ep in list:
						try:
							file = series_episode_path(ep.series, ep, config['tv']['ignored_extensions'])
						except:
							logger.warning("unable to locate '%s' on disk!", ep)
							raise
						try:
							os.remove(file)
							logger.info("removing file '%s'", file)
						except OSError, (num, message):
							if num == 13:
								logger.error("unable to delete file '%s', permission denied", file)
							raise

def _move_to_trash(root, path):

	trash_path = os.path.join(root, ".trash", os.path.basename(path))
	shutil.move(path, trash_path)

	return trash_path

