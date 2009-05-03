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
from time import strftime

from mediarover import locate_config_files
from mediarover.config import generate_config, write_config_files
from mediarover.error import *
from mediarover.series import Series
from mediarover.scripts.error import *
from mediarover.utils.configobj import ConfigObj
from mediarover.utils.filesystem import series_episode_exists, series_episode_path, series_season_path, series_season_multiepisodes, clean_path

# script version - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

version = "0.0.1"

# public methods - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

def sort():

	""" parse command line options """

	usage = "usage: %prog [options] <job_dir> <nzb> <job_name> <newzbin_id> <category> <newsgroup>"
	parser = OptionParser(usage=usage, version=version)

	# location of config dir
	parser.add_option("-c", "--config", metavar="/PATH/TO/CONFIG/DIR", help="path to application configuration directory")

	# dry run
	parser.add_option("-d", "--dry-run", action="store_true", default=False, help="simulate downloading nzb's from configured sources")

	(options, args) = parser.parse_args()

	""" config setup """

	config_dir = None
	if options.config:
		config_dir = options.config
	elif os.name == "nt":
		config_dir = os.path.expanduser("~\\Application\ Data\\mediarover")
	else: # os.name == "posix":
		config_dir = os.path.expanduser("~/.mediarover")

	# make sure application config file exists and is readable
	locate_config_files(config_dir)

	# create config object using user config values
	try:
		config = generate_config(config_dir)
	except ConfigurationError:
		exit(1)

	""" logging setup """

	# initialize and retrieve logger for later use
	# set logging path using default_log_dir from config file
	file = config['logging']['log_dir'] + "/sabnzbd_post_process_folder_sort.log"
	logging.config.fileConfig(open("%s/logging.conf" % config_dir), {"file": file})
	logger = logging.getLogger("mediarover.scripts.sabnzbd.episode")

	""" post configuration setup """

	# capture all logging output in local file.  If sorting script exits unexpectedly,
	# or encounters an error and gracefully exits, the log file will be placed in
	# the download directory for debugging
	tmp_file = os.tmpfile()
	logger.addHandler(logging.StreamHandler(tmp_file))

	""" main """

	logger.debug("using config directory: %s", config_dir)
	logger.debug("log file set to: %s", file)

	try:
		_process_download(config, options, args)
	except Exception, e:
		logger = logging.getLogger("mediarover.scripts.sabnzbd.episode")
		logger.exception(e)

		if len(args) > 0:

			# reset current position to start of file for reading...
			tmp_file.seek(0)

			# flush log data in temporary file handler to disk 
			# and exit (with error code)
			error_file = open("%s/error.log" % args[0], "w")
			error_file.write(tmp_file.read())
			error_file.flush()
			error_file.close()

		exit(1)

	exit(0)

# public methods - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

def _process_download(config, options, args):

	logger = logging.getLogger("mediarover.scripts.sabnzbd.episode")

	# check if user has requested a dry-run
	if options.dry_run:
		logger.info("--dry-run flag detected!  Download will not be sorted during execution!")

	# grab arguments passed from sabnzbd
	if not len(args) == 6:
		raise UnexpectedArgumentCount("expected 6, found %d", len(args))

	logger.debug(sys.argv[0] + " '%s' '%s' '%s' '%s' '%s' '%s'" % tuple(args))

	"""
	arguments:
	  1. The final directory of the job (full path)
	  2. The name of the NZB file
	  3. Clean version of the job name (no path info and ".nzb" removed)
	  4. Newzbin report number (may be empty
	  5. Newzbin or user-defined category
	  6. Group that the NZB was posted in e.g. alt.binaries.x
	"""
	path = args[0]
	nzb = args[1]
	job = args[2]
	report_id = args[3]
	category = args[4]
	group = args[5]

	# check to ensure we have the necessary data to proceed
	if path is None or path == "":
		raise InvalidArgument("path to completed job is missing or null")
	if job is None or job == "":
		raise InvalidArgument("job name is missing or null")
#	if category is None or category == "":
#		raise InvalidArgument("job category is missing or null")

	# make sure tv root directory exists and that we have read and 
	# write access to it
	tv_root = config['tv']['tv_root']
	if not os.path.exists(tv_root):
		raise FilesystemError("TV root directory (%s) does not exist!", (tv_root))
	if not os.access(tv_root, os.R_OK | os.W_OK):
		raise FilesystemError("Missing read/write access to tv root directory (%s)", (tv_root))

	logger.info("begin processing tv directory: %s", tv_root)

	ignore_metadata = True
	if 'ignore_series_metadata' in config['tv']:
		ignore_metadata = config['tv'].as_bool('ignore_series_metadata')

	# get list of shows in root tv directory
	shows = {}
	dir_list = os.listdir(tv_root)
	dir_list.sort()
	for name in dir_list:

		# skip hidden directories
		if name.startswith("."):
			continue

		dir = "%s/%s" % (tv_root, name)
		if os.path.isdir(dir):

			series = Series(name, path=dir, ignore_metadata=ignore_metadata)
			sanitized_name = Series.sanitize_series_name(series)

			if sanitized_name in shows:
				logger.warning("duplicate series directory found! Multiple directories for the same series can result in sorting errors!  You've been warned...")

			shows[sanitized_name] = series
			logger.debug("watching series: %s => %s", sanitized_name, dir)

	logger.info("watching %d tv show(s)", len(shows))
	logger.debug("finished processing tv directory")

	ignored = [ext.lower() for ext in config['tv']['ignored_extensions']]

	# locate episode file in given download directory
	orig_path = None
	filename = None
	extension = None
	size = 0
	for file in os.listdir(path):
		if os.path.isfile("%s/%s" % (path, file)):
			
			# check if current file's extension is in list
			# of ignored extensions
			#(name, dot, ext) = file.rpartition(".")
			(name, ext) = os.path.splitext(file)
			ext = ext.lstrip(".")
			if ext.lower() in ignored:
				continue

			# get size of current file (in bytes)
			stat = os.stat("%s/%s" % (path, file))
			if stat.st_size > size:
				filename = file
				extension = ext
				size = stat.st_size
				logger.debug("identified possible download: filename => %s, size => %d", filename, size)
	else:
		if filename is None:
			raise FilesystemError("unable to find episode file in given download path '%s'", path)

		orig_path = "%s/%s" % (path, filename)
		logger.info("found download file at '%s'", orig_path)

	# build episode object using command line values
	episode = None
	if report_id is not None and report_id != "":
		from mediarover.sources.newzbin.episode import NewzbinEpisode, NewzbinMultiEpisode
		
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
		from mediarover.episode import Episode, MultiEpisode

		try:
			if MultiEpisode.handle(job):
				episode = MultiEpisode.new_from_string(job)
			else:
				episode = Episode.new_from_string(job)

		except MissingParameterError:
			logger.error("Unable to parse job name (%s) and extract necessary values", job)
			raise

	# update new episode object with extension scrapped from downloaded file
	episode.extension = extension

	# make sure episode series object knows whether to ignore metadata or not
	episode.series.ignore_metadata = config['tv']['ignore_series_metadata']

	# determine if episode belongs to a currently watched series
	season_path = None
	additional = None
	try:
		episode.series = shows[Series.sanitize_series_name(episode.series)]

	# if not, we need to create the series directory
	except KeyError:

		logger.info("series directory not found")
		series_dir = "%s/%s" % (tv_root, episode.format_series(config['tv']['template']['series']))
		season_path = "%s/%s" % (series_dir, episode.format_season(config['tv']['template']['season']))
		try:
			os.makedirs(season_path)
			logger.info("created series directory '%s'", series_dir)
		except OSError, (num, message):
			if num == 13:
				logger.error("unable to create series directory '%s', permission denied", season_path)
			raise
		finally:
			episode.series.path = series_dir
			shows[Series.sanitize_series_name(episode.series)] = episode.series
	
	# series directory found, look for season directory
	else:

		# look for existing season directory
		try:
			season_path = series_season_path(episode.series, episode.season, config['tv']['ignored_extensions'])

		# if not, we need to create it
		except FilesystemError:
			dir = episode.format_season(config['tv']['template']['season'])
			season_path = "%s/%s" % (episode.series.path, dir)
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
	new_path = season_path + "/" + episode.format_episode(
		series_template = config['tv']['template']['series_episode'],
		daily_template = config['tv']['template']['daily_episode'], 
		smart_title_template = config['tv']['template']['smart_title'],
		additional = additional
	)

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
			# since new download was successfully moved, check if any existing downloads 
			# can now be removed
			list = []
			try:
				episode.episodes

			# single episode
			except AttributeError:
				if not config['tv']['multiepisode'].as_bool('prefer'):
					for multi in series_season_multiepisodes(episode.series, episode.season, config['tv']['ignored_extensions']):
						if episode in multi.episodes:
							for ep in multi.episodes:
								if not series_episode_exists(ep.series, ep, config['tv']['ignored_extensions']):
									break
							else:
								file = series_episode_path(multi.series, multi, config['tv']['ignored_extensions'])
								logger.debug("scheduling '%s' for deletion", file)
								list.append(file)

			# multipart episode
			else:
				if config['tv']['multiepisode'].as_bool('prefer'):
					for ep in episode.episodes:
						try:
							file = series_episode_path(ep.series, ep, config['tv']['ignored_extensions'])
						except FilesystemError:
							break
						else:
							logger.debug("scheduling '%s' for deletion", file)
							list.append(file)

			# iterate over list of stale downloads and remove them from filesystem
			finally:
				if len(list):
					for file in list:
						try:
							os.remove(file)
							logger.info("removing file '%s'", file)
						except OSError, (num, message):
							if num == 13:
								logger.error("unable to delete file '%s', permission denied", file)
							raise

			# finally, clean up download directory by removing all files matching ignored extensions list.
			# if unable to download directory (because it's not empty), move it to .trash
			try:
				clean_path(path, ignored)
				logger.info("removing download directory '%s'", path)
			except (OSError, FilesystemError):
				logger.error("unable to remove download directory '%s'", path)

				trash_path = "%s/.trash/%s" % (tv_root, os.path.basename(path))
				if not os.path.exists(trash_path):
					os.makedirs(trash_path)

				try:
					shutil.move(path, trash_path)
					logger.info("moving download directory '%s' to '%s'", path, trash_path)
				except OSError, (num, message):
					if num == 13:
						logger.error("unable to move download directory to '%s', permission denied", trash_path)
					raise

