# -*- coding: utf-8 -*-
"""
	Umbrella Add-on
"""

from ast import literal_eval
from hashlib import md5
from re import sub as re_sub
from sqlite3 import dbapi2 as db
from time import time
from resources.lib.modules import control
import sqlite3
import xbmc
import xbmcvfs
import os
import fnmatch
import threading


def get_coalesced(function, duration, *args):
	"""Share overlapping progress rebuilds across Kodi Python invocations.

	The coordination database is separate from the metadata cache: no metadata
	write lock is held while fetching remote data. Only overlapping callers use
	its result, so normal cache expiration/invalidation still applies.
	"""
	started = time()
	key = _hash_function(function, args)
	if not control.existsPath(control.dataPath): control.makeFile(control.dataPath)
	path = control.cacheFile + '.build-' + _generate_md5(key)
	con = db.connect(path, timeout=0.25)
	try:
		while True:
			try:
				con.execute('BEGIN IMMEDIATE')
				break
			except db.OperationalError as exc:
				if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower(): raise
				if control.monitor.waitForAbort(0.1): return None
		con.execute('CREATE TABLE IF NOT EXISTS result (id INTEGER PRIMARY KEY, completed REAL, value TEXT)')
		row = con.execute('SELECT completed, value FROM result WHERE id=1').fetchone()
		if row and row[0] >= started:
			control.log_refresh_diagnostic('mdb-build-shared-result')
			return literal_eval(row[1])
		result = get(function, duration, *args)
		if result is not None:
			con.execute('INSERT OR REPLACE INTO result VALUES (1, ?, ?)', (time(), repr(result)))
		con.commit()
		return result
	finally:
		con.close()


def get(function, duration, *args):
	"""
	:param function: Function to be executed
	:param duration: Duration of validity of cache in hours
	:param args: Optional arguments for the provided function
	"""
	try:
		key = _hash_function(function, args)
		cache_result = cache_get(key)
		if cache_result:
			from resources.lib.modules import log_utils
			try: result = literal_eval(cache_result['value'])
			except: result = None
			if _is_cache_valid(cache_result['date'], duration):
				return result

		fresh_result = repr(function(*args)) # may need a try-except block for server timeouts

		if cache_result and (result and len(result) == 1) and fresh_result == '[]': # fix for syncSeason mark unwatched season when it's the last item remaining
			if isinstance(result[0], str) and result[0].isdigit():
				remove(function, *args)
				return []

		invalid = False
		try: # Sometimes None is returned as a string instead of None type for "fresh_result"
			if not fresh_result: invalid = True
			elif fresh_result == 'None' or fresh_result == '' or fresh_result == '[]' or fresh_result == '{}': invalid = True
			elif len(fresh_result) == 0: invalid = True
		except: pass

		if invalid: # If the cache is old, but we didn't get "fresh_result", return the old cache
			if cache_result: return result
			else: return None # do not cache_insert() None type, sometimes servers just down momentarily
		else:
			if '404:NOT FOUND' in fresh_result:
				cache_insert(key, None) # cache_insert() "404:NOT FOUND" cases only as None type
				return None
			else: cache_insert(key, fresh_result)
			return literal_eval(fresh_result)
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		return None

def _is_cache_valid(cached_time, cache_timeout):
	now = int(time())
	diff = now - cached_time
	return (cache_timeout * 3600) > diff

_revalidate_lock = threading.Lock()
_revalidating_keys = set()
_last_auto_refresh = [0.0]
_revalidate_context = threading.local()

def get_stale(function, duration, *args, **kwargs):
	"""Stale-while-revalidate cache read.

	Returns the on-disk cached value immediately, even when it is older than
	``duration``. When the entry is stale, a background thread re-fetches and
	rewrites the cache, then reloads the current container (only when the
	request verifiably came from the visible listing and the user is still on
	it) so fresh data appears without re-entering the menu. Entries cached
	with a duration of 0 are refreshed synchronously and never trigger an
	automatic refresh. With no cached value at all there is nothing to show
	yet, so it falls back to a synchronous get().
	"""
	force = kwargs.pop('force', False)
	refresh = kwargs.pop('refresh', True)
	try:
		if force: return get(function, duration, *args) # forced updates must never return cached data
		key = _hash_function(function, args)
		cache_result = cache_get(key)
		if cache_result:
			try: result = literal_eval(cache_result['value'])
			except: result = None
			if _is_cache_valid(cache_result['date'], duration): return result
			if duration <= 0: return get(function, duration, *args) # duration 0 means always-fresh: no stale reads, no refresh loop
			if getattr(_revalidate_context, 'active', False):
				return get(function, duration, *args) # nested inside a background revalidate: fetch fresh so the outer entry cannot store stale data under a fresh timestamp
			_schedule_revalidate(function, duration, args, key, refresh=refresh)
			return result
		return get(function, duration, *args)
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		return None

def _coordination_db():
	if not control.existsPath(control.dataPath): control.makeFile(control.dataPath)
	return db.connect(control.cacheFile + '.revalidate', timeout=0.25)

def _claim_revalidate(key):
	# Dedup across separate Kodi plugin invocations through a shared sqlite
	# db, so two processes never revalidate the same key at once. The claim
	# expires so a crashed invocation cannot block revalidation forever.
	with _revalidate_lock:
		if key in _revalidating_keys: return False
		_revalidating_keys.add(key)
	try:
		con = _coordination_db()
		try:
			con.execute('CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY, claimed REAL)')
			while True:
				try:
					con.execute('BEGIN IMMEDIATE')
					break
				except db.OperationalError as exc:
					if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower(): raise
					if control.monitor.waitForAbort(0.1):
						with _revalidate_lock: _revalidating_keys.discard(key)
						return False
			now = time()
			row = con.execute('SELECT claimed FROM claims WHERE key=?', (key,)).fetchone()
			if row and now - row[0] < 300:
				con.rollback()
				with _revalidate_lock: _revalidating_keys.discard(key)
				return False
			con.execute('INSERT OR REPLACE INTO claims VALUES (?, ?)', (key, now))
			con.commit()
			return True
		finally:
			con.close()
	except:
		return True # coordination db unavailable: the in-process lock still dedups

def _release_revalidate(key):
	try:
		con = _coordination_db()
		try:
			con.execute('DELETE FROM claims WHERE key=?', (key,))
			con.commit()
		finally:
			con.close()
	except: pass

def _schedule_revalidate(function, duration, args, key, refresh=True):
	if not _claim_revalidate(key): return
	folder = _current_listing_folder()
	def work():
		try:
			if control.monitor.abortRequested(): return
			_revalidate_context.active = True
			changed = _fetch_if_changed(function, duration, args, key)
			if refresh and changed: _refresh_container_if_unchanged(folder)
		except:
			from resources.lib.modules import log_utils
			log_utils.error()
		finally:
			_revalidate_context.active = False
			with _revalidate_lock: _revalidating_keys.discard(key)
			_release_revalidate(key)
	t = threading.Thread(target=work)
	t.daemon = True
	t.start()

def _fetch_if_changed(function, duration, args, key):
	# Re-fetch and rewrite the cache, but report whether the stored value
	# actually changed, so failed requests (get() returns None or keeps the
	# old entry) never trigger a container refresh.
	try:
		cached = cache_get(key)
		cached_value = cached['value'] if cached else None
		fresh = get(function, duration, *args) # re-fetch; get() skips the write when the response is empty or failed
		if fresh is None: return False
		rewritten = cache_get(key)
		if not rewritten: return False
		return rewritten['value'] != cached_value
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		return False

def _current_listing_folder():
	# A plugin:// FolderPath alone does not prove the request came from the
	# visible listing: skin widgets also use plugin:// URLs but have no
	# plugin container and are not shown in the videos window. Require both.
	try:
		if 'plugin.video.umbrella' not in (control.infoLabel('Container.PluginName') or ''): return ''
		if not control.condVisibility('Window.IsActive(videos) or Window.IsActive(MyVideoNav.xml)'): return ''
		folder = control.infoLabel('Container.FolderPath')
		if folder and folder.startswith('plugin://plugin.video.umbrella/'): return folder
	except: pass
	return ''

def _refresh_container_if_unchanged(folder):
	# Only auto-reload when the user is still on the exact listing that asked
	# for the refresh, so a background revalidation never yanks them out of a
	# deeper menu, a playing video, or a skin widget.
	try:
		if not folder: return
		if control.player.isPlaying(): return
		if control.infoLabel('Container.FolderPath') != folder: return
		if not control.condVisibility('Window.IsActive(videos) or Window.IsActive(MyVideoNav.xml)'): return
		if not _throttled_auto_refresh(): return
		control.refresh()
	except: pass

def _throttled_auto_refresh():
	# Two-second throttle shared across separate Kodi invocations through the
	# coordination db, with the in-process timestamp as first line of defense.
	now = time()
	with _revalidate_lock:
		if now - _last_auto_refresh[0] < 2: return False
		_last_auto_refresh[0] = now
	try:
		con = _coordination_db()
		try:
			con.execute('CREATE TABLE IF NOT EXISTS auto_refresh (id INTEGER PRIMARY KEY, at REAL)')
			con.execute('BEGIN IMMEDIATE')
			row = con.execute('SELECT at FROM auto_refresh WHERE id=1').fetchone()
			if row and now - row[0] < 2:
				con.rollback()
				return False
			con.execute('INSERT OR REPLACE INTO auto_refresh VALUES (1, ?)', (now,))
			con.commit()
			return True
		finally:
			con.close()
	except:
		return True

def invalidate_lib_movies_probe():
	# Drop the cached VideoLibrary.GetMovies probe when the library changes;
	# without this the has_lib_movies menu condition could stay stale for 24h.
	try:
		from resources.lib.menus.navigator import Navigator
		remove(Navigator._lib_movies_probe)
	except:
		from resources.lib.modules import log_utils
		log_utils.error()

def timeout(function, *args):
	try:
		key = _hash_function(function, args)
		result = cache_get(key)
		return int(result['date']) if result else 0
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		return 0

def cache_existing(function, *args):
	try:
		cache_result = cache_get(_hash_function(function, args))
		if cache_result: return literal_eval(cache_result['value'])
		else: return None
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		return None

def cache_get(key):
	dbcon = None
	dbcur = None
	try:
		dbcon = get_connection()
		dbcur = get_connection_cursor(dbcon)
		ck_table = dbcur.execute('''SELECT * FROM sqlite_master WHERE type='table' AND name='cache';''').fetchone()
		if not ck_table: return None
		results = dbcur.execute('''SELECT * FROM cache WHERE key=?''', (key,)).fetchone()
		return results
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		return None
	finally:
		if dbcur: dbcur.close()
		if dbcon: dbcon.close()

def cache_insert(key, value):
	dbcon = None
	dbcur = None
	try:
		# Cache persistence is optional: return fetched data promptly when another
		# plugin invocation holds the database, rather than waiting a minute.
		dbcon = get_connection(timeout=0.25)
		dbcur = get_connection_cursor(dbcon)
		now = int(time())
		dbcur.execute('''CREATE TABLE IF NOT EXISTS cache (key TEXT, value TEXT, date INTEGER, UNIQUE(key));''')
		dbcur.execute('''INSERT OR REPLACE INTO cache Values (?, ?, ?)''', (key, value, now))
		dbcur.connection.commit()
	except db.OperationalError as exc:
		# A contended optional write must not flood the log with tracebacks.
		if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
			from resources.lib.modules import log_utils
			log_utils.error()
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
	finally:
		if dbcur: dbcur.close()
		if dbcon: dbcon.close()

def remove(function, *args):
	try:
		key = _hash_function(function, args)
		key_exists = cache_get(key)
		if key_exists:
			dbcon = get_connection()
			dbcur = get_connection_cursor(dbcon)
			dbcur.execute('''DELETE FROM cache WHERE key=?''', (key,))
			dbcur.connection.commit()
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
	try: dbcur.close() ; dbcon.close()
	except: pass

def _hash_function(function_instance, *args):
	return _get_function_name(function_instance) + _generate_md5(args)

def _get_function_name(function_instance):
	return re_sub(r'.+\smethod\s|.+function\s|\sat\s.+|\sof\s.+', '', repr(function_instance))

def _generate_md5(*args):
	md5_hash = md5()
	try: [md5_hash.update(str(arg)) for arg in args]
	except: [md5_hash.update(str(arg).encode('utf-8')) for arg in args]
	return str(md5_hash.hexdigest())

def cache_clear(flush_only=False):
	cleared = False
	dbcon = None
	dbcur = None
	try:
		dbcon = get_connection()
		dbcur = get_connection_cursor(dbcon)
		if flush_only:
			dbcur.execute('''DELETE FROM cache''')
			dbcur.connection.commit() # added this for what looks like a 19 bug not found in 18, normal commit is at end
			dbcur.execute('''VACUUM''')
			cleared = True
		else:
			dbcur.execute('''DROP TABLE IF EXISTS cache''')
			dbcur.execute('''VACUUM''')
			dbcur.connection.commit()
			cleared = True
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		cleared = False
	finally:
		if dbcur: dbcur.close()
		if dbcon: dbcon.close()
	return cleared

def clearMovieCache():
	cleared = False
	try:
		if not control.existsPath(control.dataPath): control.makeFile(control.dataPath)
		dbcon = db.connect(control.libCacheSimilar)
		dbcur = dbcon.cursor()
		dbcur.execute('''DROP TABLE IF EXISTS movies''')
		dbcur.connection.commit()
		cleared = True
	except: 
		from resources.lib.modules import log_utils
		log_utils.error()
		cleared = False
	return cleared

def get_connection(timeout=60):
	if not control.existsPath(control.dataPath): control.makeFile(control.dataPath)
	dbcon = db.connect(control.cacheFile, timeout=timeout)
	try:
		dbcon.execute('''PRAGMA page_size = 32768''')
		dbcon.execute('''PRAGMA journal_mode = OFF''')
		dbcon.execute('''PRAGMA synchronous = OFF''')
		dbcon.execute('''PRAGMA temp_store = memory''')
		dbcon.execute('''PRAGMA mmap_size = 30000000000''')
	except:
		dbcon.close()
		raise
	dbcon.row_factory = _dict_factory
	return dbcon

def get_connection_cursor(dbcon):
	dbcur = dbcon.cursor()
	return dbcur

def _dict_factory(cursor, row):
	d = {}
	for idx, col in enumerate(cursor.description): d[col[0]] = row[idx]
	return d

##################
def cache_clear_search():
	cleared = False
	try:
		dbcon = get_connection_search()
		dbcur = dbcon.cursor()
		for t in ('movies', 'tvshow', 'combined', 'collections', 'furk', 'easynews'):
			dbcur.execute('''DROP TABLE IF EXISTS {}'''.format(t))
			dbcur.execute('''VACUUM''')
			dbcur.connection.commit()
			control.refresh()
			cleared = True
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		cleared = False
	finally:
		try: dbcur.close()
		except: pass
		try: dbcon.close()
		except: pass
	return cleared

def cache_clear_SearchPhrase(table, key):
	cleared = False
	try:
		dbcon = get_connection_search()
		dbcur = dbcon.cursor()
		dbcur.execute('''DELETE FROM {} WHERE term=?;'''.format(table), (key,))
		dbcur.connection.commit()
		control.refresh()
		cleared = True
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		cleared = False
	finally:
		try: dbcur.close()
		except: pass
		try: dbcon.close()
		except: pass
	return cleared

def get_connection_search():
	control.makeFile(control.dataPath)
	conn = db.connect(control.searchFile)
	conn.row_factory = _dict_factory
	return conn
#added to clear old artwork
def cache_clear_thumbnails():
	cleared = False
	thumbnailsPath = xbmcvfs.translatePath('special://profile/Thumbnails/')
	databasePath  = xbmcvfs.translatePath('special://profile/Database/')
	addonPath = xbmcvfs.translatePath('special://home/addons')
	try:
		dbcon = sqlite3.connect(databasePath+'/Textures13.db')
		dbcur = dbcon.cursor()
		icon = dbcur.execute("SELECT cachedurl FROM texture WHERE url ='" + addonPath + "/plugin.video.umbrella/icon.png';").fetchone()
		fanart = dbcur.execute("SELECT cachedurl FROM texture WHERE url ='" + addonPath + "/plugin.video.umbrella/fanart.jpg';").fetchone()
		if icon is not None:
			if xbmcvfs.exists(thumbnailsPath + icon[0]):
				xbmcvfs.delete(thumbnailsPath + icon[0])
			dbcur.execute("DELETE FROM texture WHERE url ='" + addonPath + "/plugin.video.umbrella/icon.png';") #delete the icon
		if fanart is not None:
			if xbmcvfs.exists(thumbnailsPath + fanart[0]):
				xbmcvfs.delete(thumbnailsPath + fanart[0])
			dbcur.execute("DELETE FROM texture WHERE url ='" + addonPath + "/plugin.video.umbrella/fanart.jpg';") #delete the fanart
		dbcon.close()
		cleared = True
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		cleared = False
	return cleared

##################
def cache_clear_bookmarks():
	cleared = False
	try:
		dbcon = get_connection_bookmarks()
		dbcur = dbcon.cursor()
		dbcur.execute('''DROP TABLE IF EXISTS bookmark''')
		dbcur.execute('''VACUUM''')
		dbcur.connection.commit()
		cleared = True
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		cleared = False
	finally:
		try: dbcur.close()
		except: pass
		try: dbcon.close()
		except: pass
	return cleared

def cache_clear_bookmark(name, year='0'):
	cleared = False
	try:
		dbcon = get_connection_bookmarks()
		dbcur = dbcon.cursor()
		# idFile = md5()
		# for i in name: idFile.update(str(i))
		# for i in year: idFile.update(str(i))
		# idFile = str(idFile.hexdigest())
		# dbcur.execute("DELETE FROM bookmark WHERE idFile = '%s'" % idFile)
		years = [str(year), str(int(year)+1), str(int(year)-1)]
		dbcur.execute('''DELETE FROM bookmark WHERE Name="%s" AND year IN (%s)''' % (name, ','.join(i for i in years)))
		dbcur.connection.commit()
		control.refresh()
		control.trigger_widget_refresh()
		cleared = True
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		cleared = False
	finally:
		try: dbcur.close()
		except: pass
		try: dbcon.close()
		except: pass
	return cleared

def get_connection_bookmarks():
	control.makeFile(control.dataPath)
	conn = db.connect(control.bookmarksFile)
	conn.row_factory = _dict_factory
	return conn
##################
def clear_local_bookmarks(): # clear all umbrella bookmarks from kodi database
	dbcon = dbcur = None
	try:
		dbcon = db.connect(get_video_database_path())
		dbcur = dbcon.cursor()
		dbcur.execute('''SELECT * FROM files WHERE strFilename LIKE "%plugin.video.umbrella%"''')
		file_ids = [str(i[0]) for i in dbcur.fetchall()]
		if not file_ids: return
		for table in ('bookmark', 'streamdetails', 'files'):
			dbcur.execute('''DELETE FROM {} WHERE idFile IN ({})'''.format(table, ','.join(file_ids)))
		dbcur.connection.commit()
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
	finally:
		try:
			if dbcur: dbcur.close()
			if dbcon: dbcon.close()
		except: pass

def clear_local_bookmark(url): # clear all item specific bookmarks from kodi database
	try:
		dbcon = db.connect(get_video_database_path())
		dbcur = dbcon.cursor()
		dbcur.execute('''SELECT * FROM files WHERE strFilename LIKE "%{}%"'''.format(url))
		file_ids = [str(i[0]) for i in dbcur.fetchall()]
		if not file_ids: return
		for table in ('bookmark', 'streamdetails', 'files'):
			dbcur.execute('''DELETE FROM {} WHERE idFile IN ({})'''.format(table, ','.join(file_ids)))
		dbcur.connection.commit()
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
	finally:
		try: dbcur.close()
		except: pass
		try: dbcon.close()
		except: pass

def get_video_database_path():
	databaseFound = False
	try:
		database_path = control.absPath(control.joinPath(control.dataPath, '..', '..', 'Database', )) # doesn't work with mysql
		# path_db = 'special://profile/Database/%s' % db_name
		def find(pattern, path):
			result = []
			for root, dirs, files in os.walk(path):
				for name in files:
					if fnmatch.fnmatch(name, pattern):
						result.append(os.path.join(root, name))
			return result
		databasefile = find('MyVideos*.db', database_path)
		databasefile.sort(reverse=True)
		database_path_final = databasefile[0]
		#from resources.lib.modules import log_utils
		#log_utils.log('Umbrella MyVideos file path: %s' % str(database_path_final), 1)
		databaseFound = True
	except:
		#from resources.lib.modules import log_utils
		#log_utils.log('Umbrella MyVideos file path exception manual selection needed', 1)
		databaseFound = False
	if databaseFound == False:
		kodi_version = control.getKodiVersion()
		if kodi_version == 17: database_path_final = control.joinPath(database_path, 'MyVideos107.db')
		elif kodi_version == 18: database_path_final = control.joinPath(database_path, 'MyVideos116.db')
		elif kodi_version == 19: database_path_final = control.joinPath(database_path, 'MyVideos119.db')
		elif kodi_version == 20: database_path_final = control.joinPath(database_path, 'MyVideos121.db')
		elif kodi_version == 21: database_path_final = control.joinPath(database_path, 'MyVideos131.db')
	return database_path_final
##################

def clrCache_version_update(clr_providers=False, clr_metacache=False, clr_cache=False, clr_search=False, clr_bookmarks=False):
	try:
		if clr_providers:
			from resources.lib.database import providerscache
			providerscache.cache_clear_providers()
		if clr_metacache:
			from resources.lib.database import metacache
			metacache.cache_clear_meta()
		if clr_cache: cache_clear(flush_only=True)
		if clr_search: cache_clear_search()
		if clr_bookmarks: cache_clear_bookmarks()
		control.notification(message='Forced cache clear for version update complete.')
		from resources.lib.modules import log_utils
		log_utils.log('Forced cache clear for version update complete.', level=log_utils.LOGDEBUG)
	except:
		from resources.lib.modules import log_utils
		log_utils.error()

def update_cache_version():
	versionFile = control.joinPath(control.dataPath, 'cache.v')
	try:
		if not control.existsPath(versionFile):
			f = open(versionFile, 'w')
			f.close()
	except:
		from resources.lib.modules import log_utils
		log_utils.log('Umbrella Addon Data Path Does not Exist. Creating Folder....', __name__, level=log_utils.LOGDEBUG)
		ad_folder = control.transPath('special://profile/addon_data/plugin.video.umbrella')
		control.makeDirs(ad_folder)
	try:
		with open(versionFile, 'r') as fh: oldVersion = fh.read()
	except: oldVersion = '0'
	try:
		curVersion = control.addon('plugin.video.umbrella').getAddonInfo('version')
		if oldVersion != curVersion:
			with open(versionFile, 'w') as fh: fh.write(curVersion)
			return oldVersion, True
		else: return oldVersion, False
	except:
		from resources.lib.modules import log_utils
		log_utils.error()
		return oldVersion, False

def get_cache_version():
	versionFile = control.joinPath(control.dataPath, 'cache.v')
	try:
		if not control.existsPath(versionFile):
			f = open(versionFile, 'w')
			f.close()
	except:
		from resources.lib.modules import log_utils
		log_utils.log('Umbrella Addon Data Path Does not Exist. Creating Folder....', __name__, log_utils.LOGDEBUG)
		ad_folder = control.transPath('special://profile/addon_data/plugin.video.umbrella')
		control.makeDirs(ad_folder)
	try:
		with open(versionFile, 'r') as fh: oldVersion = fh.read()
	except: oldVersion = '0'
	return oldVersion
