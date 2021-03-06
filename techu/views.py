import os, sys, datetime, codecs
import json, time, math
import string, hashlib
import marshal
from django.http import HttpResponse
from django.shortcuts import render
from django.db import IntegrityError, DatabaseError
from django.db import connections, transaction
from django.db import models
from django.core import serializers
from libraries.generic import *
from techu.models import *
from libraries.sphinxapi import *
from libraries.caching import Cache
import settings 

modules = None

def _import(module_list):
  ''' 
  Dynamic imports may boost performance since functions require different packages 
  e.g. libraries.sphinxapi is only used for search and excerpts calls
  '''
  global modules
  for m in map(__import__, module_list):
    if not m in modules:
      modules.append(m)

def _error(code = 500, **kwargs):
  ''' Return an HttpResponse with error code '''
  response = HttpResponse()
  response.status_code = code
  if 'message' in kwargs:
    message = kwargs['message']
  else:
    message = 'Internal Server Error'
  response.content = message
  return response

def _response(data, code = 200, serialize = True):
  ''' 
  Return a successful, normal HttpResponse (code 200). 
  Serializes by default any object passed.
  '''
  r = HttpResponse()
  r.status_code = code
  if serialize:
    is_object = isinstance(data, models.query.QuerySet)
    if isinstance(data, models.query.QuerySet) or (isinstance(data, list) and (isinstance(data[0], models.query.QuerySet))):
      data = serializers.serialize('json', data)
    else:
      data = json.dumps(data)
  r.content = data
  r.content_type = 'application/json;charset=utf-8'
  return r

def debug(r):
  ''' Serialize and return object for debugging '''
  return _response(r)

def home(request):
  ''' Home/Index page '''
  return HttpResponse("<h1>Techu Indexing Server</h1>\n")

def option_list(request):
  return _response(Option.objects.filter())

def option(request, section, section_instance_id):
  ''' Connect options with searchd, indexes & sources and store their values '''
  section = section.lower()
  r = request_data(request)
  data = json.loads(r['data'])
  options = Option.objects.filter(name__in = data.keys())
  options_stored = []
  for option in options:
    if not isinstance(data[option.name], list):
      values = [data[option.name]]
    else:
      values = data[option.name]
    for value in values:
      value = unicode(value)
      value_hash = hashlib.md5(value).hexdigest()
      if section == 'searchd':      
        o = SearchdOption.objects.create(
              sp_searchd_id = section_instance_id, 
              sp_option_id = option.id, 
              value = value,
              value_hash = value_hash)
      elif section == 'index':
        o = IndexOption.objects.create(
              sp_index_id = section_instance_id,
              sp_option_id = option.id,
              value = value,
              value_hash = value_hash)
      elif section == 'source':
        o = SourceOption.objects.create(
              sp_source_id = section_instance_id,
              sp_option_id = option.id,
              value = value,
              value_hash = value_hash)
      options_stored.append(o.id)
  if section == 'searchd':    
    options_stored = SearchdOption.objects.filter(id__in = options_stored)
  elif section == 'index':    
    options_stored = IndexOption.objects.filter(id__in = options_stored) 
  elif section == 'source':    
    options_stored = SourceOption.objects.filter(id__in = options_stored)
  return _response(options_stored)

def index(request, index_id = 0):
  ''' Add or modify information for an index '''
  r = request_data(request)
  fields = model_fields(Index, r)
  if index_id == 0:
    try:
      i = Index.objects.create(**fields)
      if 'conf_id' in r:
        ConfigurationIndex.objects.create(sp_index_id = i.id, sp_configuration_id = r['conf_id'], is_active = 1)
      i = Index.objects.filter(pk = i.id)
    except IntegrityError as e:
      Index.objects.filter(name = fields['name']).update(**fields)
      i = Index.objects.get(name = fields['name'])
  else:
    try:
      i = Index.objects.get(pk = index_id)
    except:
      return _error()
  return _response(i)

def index_list(request):
  ''' Return a JSON Array with all indexes '''
  return _response(Index.objects.all())

def configuration_list(request):
  ''' Return a JSON Array with all configurations '''
  return _response( Configuration.objects.all())

def searchd(request, searchd_id = 0):
  ''' Store a new searchd '''
  r = request_data(request)
  fields = model_fields(Searchd, r)
  if searchd_id > 0:
    s = Searchd.objects.filter(pk = searchd_id).update(**fields)
  else:
    s = Searchd.objects.create(**fields)
    searchd_id = s.id
    s = Searchd.objects.filter(pk = searchd_id)
  if 'conf_id' in r:
    cs = ConfigurationSearchd.objects.create(sp_configuration_id = int(r['conf_id']), sp_searchd_id = searchd_id)
  return _response(s)

def configuration(request, conf_id = 0):
  ''' Get or update information for a configuration '''
  r = request_data(request)
  fields = model_fields(Configuration, r)
  if conf_id > 0:
    c = Configuration.objects.get(pk = conf_id)
  else:
    if not regex_check(r['name']):
      return _error('Illegal configuration name "%s"' % r['name'])
    try:
      c = Configuration.objects.create(**fields)
      c.hash = hashlib.md5(str(c.id) + c.name).hexdigest()
      c.save(update_fields = ['hash'])
      c = Configuration.objects.filter(pk = c.id)
    except Exception as e:
      return _error(str(e))
    except IntegrityError as e:
      return _error('IntegrityError: ' + str(e))
  return _response(c)

def batch_indexer(request, action, index_id):
  '''
  Bulk indexing 
  '''
  action = action.lower()
  r = request_data(request)
  if 'queue' in r:
    queue = (int(r['queue']) == 1)
    del r['queue']
  try:
    data = json.loads(r['data'])
  except:
    return _error(message = 'Invalid JSON document passed with "data" parameter')
  if not isinstance(data, list):
    data = [data]
  responses = []
  if action == 'insert':
    values = []
    fields = data[0].keys()
    for document in data:
      values.append(document.values())
    responses.append( insert(index_id, fields, values, queue) )
  elif action == 'update':
    fields = data[0].keys()
    fields.remove('id')
    for document in data:
      doc_id = document['id']
      responses.append( update(index_id, doc_id, fields, [ document[field] for field in fields ], queue) )
  elif action == 'delete':
    for document in data:
      responses.append(delete(index_id, document['id'], queue))
  else:
    return _error(message = 'Unknown action. Valid types are [ insert, update, delete ]')
  return _response(responses)    

def indexer(request, action, index_id, doc_id = 0):
  ''' Add, delete, update documents '''
  action = action.lower()
  r = request_data(request)
  if 'data' in r:
    data = json.loads(r['data'])
    if 'id' in data and doc_id == 0:
      doc_id = int(data['id'])
  queue = False  
  if 'queue' in r:
    queue = (int(r['queue']) == 1)
    del r['queue']
  if action == 'insert':
    response = insert(index_id, data.keys(), [ data.values() ], queue)
  elif action == 'update':
    response = update(index_id, doc_id, data.keys(), data.values(), queue) 
  elif action == 'delete':
    response = delete(index_id, doc_id, queue) 
  else:
    return _error('Invalid action "%s"' % (action,))
  return _response(response)

def insert(index_id, fields, values, queue = True):
  ''' 
  Build INSERT statement. 
  Supports multiple VALUES sets for batch inserts.
  '''
  index = fetch_index_name(index_id)
  sql  = "INSERT INTO %s(%s) VALUES" % (index, ',' . join(fields))
  sql += '(' + ','.join([ '%s' for v in values[0] ]) + ')'
  '''
  Possible issue when quoting signed rt_attr_bigint values (could this originate from 32-bit systems arch?)
  '''
  return modify_index(index_id, sql, queue, values)

def delete(index_id, doc_id, queue = True):
  ''' Build DELETE statement '''
  index = fetch_index_name(index_id)
  sql = 'DELETE FROM ' + identq(index) + ' WHERE id = %d' % (int(doc_id),)
  return modify_index(index_id, sql, queue)

def update(index_id, doc_id, fields, values, queue = True):
  ''' Build UPDATE statement '''
  index = fetch_index_name(index_id)
  sql = 'UPDATE %s SET ' % (identq(index),)
  for n, v in enumerate(values):
    sql += fields[n] + ' = %s,'
  sql = sql.rstrip(',') + ' WHERE id = ' + str(int(doc_id))
  return modify_index(index_id, sql, queue, values)

def modify_index(index_id, sql, queue, values = None, retries = 0):
  ''' 
  Either adds to index directly or queues statements 
  for async execution by storing them in Redis 
  If either Redis or searchd is unresponsive MAX_RETRIES attempts will be performed 
  in order to store the request to the alternative
  '''
  if retries > settings.MAX_RETRIES: 
    return _error(message = 'Maximum retries %d exceeded' % MAX_RETRIES)
  queue_action = None
  if sql.find('INSERT') == 0:
    queue_action = 'insert'
  elif sql.find('UPDATE') == 0:
    queue_action = 'update'
  elif sql.find('DELETE') == 0:
    queue_action = 'delete'
  response = None
  cache = Cache()
  if not queue:
    try:
      c = connections['sphinx:' + index]
      cursor = c.cursor()
      if queue_action == 'delete':
        cursor.execute( sql )
      elif queue_action == 'update':
        cursor.execute(sql, values)
      elif queue_action == 'insert':
        cursor.executemany(sql, values)
      cache.dirty(index_id)
      response = { 'searchd' : 'ok' }
    except Exception as e:
      response = modify_index(index_id, sql, True, values, retries + 1)
  else:
    try:
      rkey = rqueue(queue_action, index_id, sql, values)
      response = { 'redis' : rkey }
    except Exception as e:
      response = modify_index(index_id, sql, False, values, retries + 1)
  return response

def fetch_index_name(index_id):
  ''' Fetch index name by id '''
  try:
    return Index.objects.filter(pk = index_id).values()[0]['name']
  except Exception as e:
    return _error(message = 'No such index')

def rqueue(queue, index_id, sql, values):
  '''
  Redis queue for incoming requests
  Applier daemon continuously reads from this queue 
  and executes asynchronously 
  TODO: check if it works better with Pub/Sub
  '''
  r = redis26()
  c = r.incr(settings.TECHU_COUNTER)
  request_time = int(time.time()*10**6)
  key = ':' . join(map(str, [ queue, index, request_time, c ]))
  if queue == 'delete':
    data = { 'sql' : sql, 'values' : [] }
  else:
    data = { 'sql' : sql, 'values' : values }
  ''' marshal serialization is much faster than JSON '''
  data = marshal.dumps(data)
  ''' Transaction '''
  p = r.pipeline()
  p.rpush('queue:' + str(index_id), key)
  p.set(key, data)
  p.execute()
  return key

def search(request, index_id):
  cache = Cache()
  index = fetch_index_name(index_id)
  ''' Search wrapper with SphinxQL '''
  r = request_data(request)
  if 'data' in r:
    r = r['data']
  if settings.SEARCH_CACHE:
    cache_key = hashlib.md5(index + r).hexdigest()
    lock_key = 'lock:' + cache_key
    version = cache.version(index_id)
    cache_key = 'cache:search:%s:%d:%s' % (cache_key, index_id, version)
    try:   
      response = cache.get(cache_key) 
      if not response is None:
        return _response(response, 200, False)
      else:
        ''' lock this key for re-caching '''
        start = time.time()
        lock = cache.get(lock_key)
        while ( not lock is None ):
          lock = cache.get(lock_key)
          if (time.time() - start) > settings.CACHE_LOCK_TIMEOUT:
            return _error(message = 'Cache lock wait timeout exceeded')
        ''' check if key now exists in cache '''
        response = cache.get(cache_key)
        if not response is None:
          return _response(response, 200, False)
        ''' otherwise acquire lock for this session '''
        cache.set(lock_key, 1, True, settings.CACHE_LOCK_TIMEOUT) # expire in 10sec        
    except:
      pass    
  
  r = json.loads(r)  
  option_mapping = {
    'mode' : {
        'extended' : SPH_MATCH_EXTENDED2,
        'boolean'  : SPH_MATCH_BOOLEAN,
        'all'      : SPH_MATCH_ALL,
        'phrase'   : SPH_MATCH_PHRASE,
        'fullscan' : SPH_MATCH_FULLSCAN,
        'any'      : SPH_MATCH_ANY,
      }
  }
  options = {
      'sortby'      : '',
      'mode'        : 'extended',
      'groupby'     : '',
      'groupsort'   : '',
      'offset'      : 0,
      'limit'       : 1000,
      'max_matches' : 0,
      'cutoff'      : 0,
      'fields'      : '*',
    }
  
  sphinxql_list_options = {
    'ranker' : [ 'proximity_bm25', 'bm25', 'none', 'wordcount', 'proximity',
                 'matchany', 'fieldmask', 'sph04', 'expr', 'export' ],
    'idf' : [ 'normalized', 'plain'],
    'sort_method'  : ['pq', 'kbuffer' ]
  }
  sphinxql_options = { 
    'agent_query_timeout' : 10000,
    'boolean_simplify' : 0,
    'comment' : '',
    'cutoff'  : 0,
    'field_weights' : '',
    'global_idf' : '',
    'idf' : 'normalized',
    'index_weights'  : '',
    'max_matches' : 10000,
    'max_query_time' : 10000,
    'ranker' : 'proximity_bm25',
    'retry_count' : 2,
    'retry_delay' : 100,
    'reverse_scan' : 0,
    'sort_method'  : 'pq'
  }
  order_direction = {
    '-1'   : 'DESC',
    'DESC' : 'DESC',
    '1'    : 'ASC',
    'ASC'  : 'ASC',
  }

  try:
    ''' Check attributes from request with stored options (sp_index_option) '''
    ''' Preload host and ports per index '''
    ''' Support query batch (RunQueries) '''
    '''
    SELECT
    select_expr [, select_expr ...]
    FROM index [, index2 ...]
    [WHERE where_condition]
    [GROUP BY {col_name | expr_alias}]
    [WITHIN GROUP ORDER BY {col_name | expr_alias} {ASC | DESC}]
    [ORDER BY {col_name | expr_alias} {ASC | DESC} [, ...]]
    [LIMIT [offset,] row_count]
    [OPTION opt_name = opt_value [, ...]]
    '''
    sql_sequence = [ ('SELECT', 'fields'), ('FROM', 'indexes'), ('WHERE', 'where'), 
                     ('GROUP BY', 'group_by'), ('WITHIN GROUP ORDER BY', 'order_within_group'), 
                     ('ORDER BY', 'order_by'), ('LIMIT', 'limit'), ('OPTION', 'option') ]
    sql = {}
    for sql_clause, key in sql_sequence:
      sql[key] = ''
      if not key in r:
        r[key] = ''
    sql['indexes'] = index + ','.join( r['indexes'] )
    if isinstance(r['fields'], list):
      sql['fields'] = ',' . join(r['fields'])
    else:
      sql['fields'] = options['fields']
    if r['group_by'] != '':
      sql['group_by'] = r['groupby']
    if not isinstance(r['limit'], dict):
      r['limit'] = { 'offset' : '0', 'count' : options['limit'] }
    r['limit'] = '%(offset)s, %(count)s' % r['limit']
    sql['order_by'] = ',' . join([ '%s %s' % (order[0], order_direction(order[1].upper())) for order in r['order_by'] ])
    if r['order_within_group'] != '':
      sql['order_within_group'] = ',' . join([ '%s %s' % (order[0], order_direction(order[1].upper())) for order in r['order_within_group'] ])
    sql['where'] = [] #dictionary e.g. { 'date_from' : [[ '>' , 13445454350] ] } 
    value_list = []
    if isinstance(r['where'], dict):
      for field, conditions in r['where'].iteritems():
        for condition in conditions:
          operator, value = condition
          value_list.append(value)
          sql['where'].append('%s%s%%s' % (field, operator,))
    value_list.append(r['q'])
    sql['where'].append('MATCH(%%s)')
    sql['where'] = ' ' . join(sql['where'])
    if isinstance(r['option'], dict):
      sql['option'] = []
      for option_name, option_value in r['option'].iteritems():
        if isinstance(option_value, dict): 
          option_value = '(' + (','. join([ '%s = %s' % (k, option_value[k]) for k in option_value.keys() ])) + ')'
          sql['option'].append('%s = %s' % (option_name, option_value))
      sql['option'] = ',' . join(sql['option'])
    response = { 'results' : None, 'meta' : None }
    try:    
      cursor = connections['sphinx:' + index].cursor()
      sql =  ' ' . join([ clause[0] + ' ' + sql[clause[1]] for clause in sql_sequence if sql[clause[1]] != '' ]) 
      cursor.execute(sql, value_list)
      response['results'] = cursorfetchall(cursor)
    except Exception as e:
      error_message = 'Sphinx Search Query failed with error "%s"' % str(e)
      return _error(message = error_message)
    try:
      cursor.execute('SHOW META')
      response['meta'] = cursorfetchall(cursor)
    except:
      pass
    if settings.SEARCH_CACHE:
      cache.set(cache_key, response, True, SEARCH_CACHE_EXPIRE, lock_key)
  except Exception as e:
    return _error(message = str(e))
  return _response(response)

def excerpts(request, index_id):
  cache = Cache()
  ''' 
  --- FEATURE UNDER CONSTRUCTION ---
  Returns highlighted snippets 
  Caches responses in Redis
  '''
  index = fetch_index_name(index_id)
  r = request_data(request)
  cache_key = hashlib.md5(index + r['data']).hexdigest()
  lock_key = 'lock:' + cache_key
  version = cache.version(index_id)
  cache_key = 'cache:excerpts:%s:%d:%s' % (cache_key, index_id, version)
  if settings.EXCERPTS_CACHE:
    try:   
      response = cache.get(cache_key) 
      if not response is None:
        return _response(response, 200, False) 
      ''' lock this key for re-caching '''
      start = time.time()
      lock = cache.get(lock_key)
      while ( not lock is None ):
        lock = cache.get(lock_key)
        if (time.time() - start) > settings.CACHE_LOCK_TIMEOUT:
          return _error(message = 'Cache lock wait timeout exceeded')
      ''' check if key now exists in cache '''
      response = cache.get(cache_key)
      if not response is None:
        return _response(response, 200, False)
      ''' otherwise acquire lock for this session '''
      cache.set(lock_key, 1, True, settings.CACHE_LOCK_TIMEOUT) # expire in 10sec         
    except:
      pass    
  else:
    r = json.loads(r['data'])

  options = {
      "before_match"      : '<b>',
      "after_match"       : '</b>',
      "chunk_separator"   : '...',
      "limit"             : 256,
      "around"            : 5,    
      "exact_phrase"      : False,
      "use_boundaries"    : False,
      "query_mode"        : False,
      "weight_order"      : False,
      "force_all_words"   : False,
      "limit_passages"    : 0,
      "limit_words"       : 0,
      "start_passage_id"  : 1,
      "html_strip_mode"   : 'index',
      "allow_empty"       : False,
      "passage_boundary"  : 'paragraph',
      "emit_zones"        : False
  }
  for k, v in options.iteritems():
    if k in r:
      if isinstance(v, int):
        options[k] = int(r[k])
      elif isinstance(v, bool):
        options[k] = bool(r[k])
      else:
        options[k] = r[k]
  if 'ttl' in r:      
    cache_expiration = int(r['ttl'])
  else:
    cache_expiration = settings.EXCERTS_CACHE_EXPIRE
  if isinstance(r['docs'], dict):
    document_ids = r['docs'].keys()
    documents = r['docs'].values()
  elif isinstance(r['docs'], list):
    document_ids = range(len(r['docs'])) # get a list of numeric indexes from the list
    documents = r['docs']
  else:
    return _error('Documents are passed as a list or dictionary structure')
  del r['docs'] # free up some memory
  '''
  docs = { 838393 : 'a document with lots of text', 119996 : 'another document with text' }
  '''
  ci = ConfigurationIndex.objects.filter(sp_index_id = index_id)
  searchd_id = ConfigurationSearchd.objects.filter(sp_configuration_id = ci.sp_configuration_id)
  so = SearchdOption.objects.filter(sp_searchd_id = searchd_id, sp_option_id = 138,).exclude(value__endswith = ':mysql41')
  sphinx_port = int(so.value)
  so = SearchdOption.objects.filter(sp_searchd_id = searchd_id, sp_option_id = 188,)
  try:
    sphinx_host = so.value
  except:
    sphinx_host = 'localhost'
  try:
    cl = SphinxClient()
    cl.SetServer(host = sphinx_host, port = sphinx_port)
    excerpts = cl.BuildExcerpts( documents, index, r['q'], options)
    del documents
    if not excerpts:
      return _error(message = 'Sphinx Excerpts Error: ' + cl.GetLastError())
    else:      
      cache_key = ''
      if settings.EXCERPTS_CACHE:
        cache.set(cache_key, excerpts, True, cache_expiration, lock_key)
      excerpts = { 
        'excerpts' : dict(zip(document_ids, excerpts)), 
        'cache-key' : cache_key,        
        }
      return _response(json.dumps(excerpts), 200, False)
  except Exception as e:
    return _error(message = 'Error while building excerpts ' + str(e))

def generate(request, configuration_id):
  ''' 
  Generate configuration file and restart searchd 
  Response contains a dictionary with the configuration file contents, 
  the stop/start commands and status
  '''
  searchd_start = 'searchd --config %(config)s %(switches)s'
  searchd_stop  = 'searchd --config %(config)s --stopwait'
  params = {}
  params['switches'] = ' '.join([ '--iostats', '--cpustats' ])
  c = Configuration.objects.get(pk = configuration_id)
  params['config'] = os.path.join(settings.PROJECT_ROOT, 'sphinx-conf', c.name) + '.conf'
  ci = ConfigurationIndex.objects.filter(sp_configuration_id = configuration_id).exclude(is_active = 0)
  si = ConfigurationSearchd.objects.filter(sp_configuration_id = configuration_id)
  searchd_options = SearchdOption.objects.filter(sp_searchd_id = si[0].sp_searchd_id)
  option_list = [ option.sp_option_id for option in searchd_options ]  
  indexes = Index.objects.filter(id__in = [ index.sp_index_id for index in ci ]).exclude(is_active = 0)
  parent_indexes = Index.objects.filter(id__in = [ index.parent_id for index in indexes ])
  index_options = IndexOption.objects.filter(sp_index_id__in = [ index.id for index in indexes ] + [index.id for index in parent_indexes ] )
  option_list += [ option.sp_option_id for option in index_options ]
  options = Option.objects.filter(id__in = option_list).values()
  option_names = {}
  for o in options:
    option_names[o['id']] = o['name']
  configuration = []
  for index in indexes:
    parent_name = ''
    if index.parent_id > 0:
      for pi in parent_indexes:
        if pi.id == index.parent_id:
          parent_name = ':' + pi.name
    index_name = index.name + parent_name
    configuration.append('index ' + index_name + ' {')
    for option in index_options:
      if option.sp_index_id == index.parent_id:
        configuration.append('  %s = %s' % ( unicode(option_names[option.sp_option_id]).ljust(30), unicode(option.value)))
      if option.sp_index_id == index.id:
        configuration.append('  %s = %s' % ( unicode(option_names[option.sp_option_id]).ljust(30), unicode(option.value)))
    configuration.append('}')
  
  configuration.append('searchd {')
  for option in searchd_options:    
    configuration.append('  %s = %s' % ( unicode(option_names[option.sp_option_id].ljust(30)), unicode(option.value)))
  configuration.append('}')
  configuration.append("")
  configuration = "\n" . join(configuration)
  f = codecs.open(params['config'], 'w', 'utf-8')
  f.write(configuration)
  f.close()
  try:
    stopped = os.system(searchd_stop % params)
    started = os.system(searchd_start % params)
  except Exception as e:
    return _error('Error while restarting searchd ' + str(e))
  response = { 
    'configuration' : configuration, 
    'stopped' : { 'command' : searchd_stop % params,  'status' : not bool(stopped) }, 
    'started' : { 'command' : searchd_start % params, 'status' : not bool(started) },
    }
  return _response(response)

