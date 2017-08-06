# pyfc4

import copy
import datetime
import io
import json
import rdflib
from rdflib.compare import to_isomorphic, graph_diff
import rdflib_jsonld
import requests
import time
from types import SimpleNamespace
import uuid

# logging
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)



class Repository(object):
	
	'''
	Class for Fedora Commons 4 (FC4), LDP server instance

	Args:
		root (str): Full URL of repository REST endpoint (e.g. http://localhost:8080/rest)
		username (str): username for authorization and roles
		password (str): password authorziation and roles
		context (dict): dictionary of namespace prefixes and namespace URIs that propagate to Resources 
		default_serialization (str): mimetype of default Accept and Content-Type headers

	Attributes:
		context (dict): Default dictionary of namespace prefixes and namespace URIs
	'''

	context = {
		'premis':'http://www.loc.gov/premis/rdf/v1#',
		'test':'info:fedora/test/',
		'rdfs':'http://www.w3.org/2000/01/rdf-schema#',
		'dbpedia':'http://dbpedia.org/ontology/',
		'xsi':'http://www.w3.org/2001/XMLSchema-instance',
		'xmlns':'http://www.w3.org/2000/xmlns/',
		'rdf':'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
		'fedora':'http://fedora.info/definitions/v4/repository#',
		'xml':'http://www.w3.org/XML/1998/namespace',
		'ebucore':'http://www.ebu.ch/metadata/ontologies/ebucore/ebucore#',
		'ldp':'http://www.w3.org/ns/ldp#',
		'xs':'http://www.w3.org/2001/XMLSchema',
		'fedoraconfig':'http://fedora.info/definitions/v4/config#',
		'foaf':'http://xmlns.com/foaf/0.1/',
		'dc':'http://purl.org/dc/elements/1.1/'
	}

	def __init__(self, 
			root,
			username,
			password,
			context = None,
			default_serialization = 'application/rdf+xml',
		):

		# handle root path
		self.root = root
		if not self.root.endswith('/'): # ensure trailing slash
			self.root += '/'
		self.username = username
		self.password = password

		# serialization
		self.default_serialization = default_serialization

		# API facade
		self.api = API(self)

		# if context provided, merge with defaults
		if context:
			logger.debug('context provided, merging with defaults')
			self.context.update(context)

		# container for transactions
		self.txns = {}


	def parse_uri(self, uri=None):
	
		'''
		parses and cleans up possible uri inputs, return instance of rdflib.term.URIRef

		Args:
			uri (rdflib.term.URIRef,str): input URI

		Returns:
			rdflib.term.URIRef
		'''

		# no uri provided, assume root
		if not uri:
			return rdflib.term.URIRef(self.root)

		# string uri provided
		elif type(uri) == str:

			# assume "short" uri, expand with repo root
			if type(uri) == str and not uri.startswith('http'):
				return rdflib.term.URIRef("%s%s" % (self.root, uri))

			# else, assume full uri
			else:
				return rdflib.term.URIRef(uri)

		# already rdflib.term.URIRef
		elif type(uri) == rdflib.term.URIRef:
			return uri

		# unknown input
		else:
			raise TypeError('invalid URI input')


	def create_resource(self, resource_type=None, uri=None):

		'''
		Convenience method for creating a new resource

		Note: A Resource is instantiated, but is not yet created.  Still requires resource.create().

		Args:
			uri (rdflib.term.URIRef, str): uri of resource to create
			resource_type (NonRDFSource (Binary), BasicContainer, DirectContainer, IndirectContainer):  resource type to create

		Returns:
			(NonRDFSource (Binary), BasicContainer, DirectContainer, IndirectContainer): instance of appropriate type
		'''

		if resource_type in [NonRDFSource, Binary, BasicContainer, DirectContainer, IndirectContainer]:
			return resource_type(self, uri)
		else:
			raise TypeError("expecting Resource type, such as BasicContainer or NonRDFSource")


	def get_resource(self, uri, response_format=None):

		'''
		return appropriate Resource-type instance
			- issue HEAD request, determine Resource type (BasicContainer, DirectContainer, IndirectContainer, or NonRDFSource (Binary))
			- issue GET request and retrieve resource metadata at uri/fcr:metadata

		Args:
			uri (rdflib.term.URIRef,str): input URI
			response_format (str): expects mimetype / Content-Type header such as 'application/rdf+xml', 'text/turtle', etc.

		Returns:
			Resource
		'''

		# handle uri
		uri = self.parse_uri(uri)

		# remove fcr:metadata if included, as handled below
		if uri.toPython().endswith('/fcr:metadata'):
			uri = rdflib.term.URIRef(uri.toPython().rstrip('/fcr:metadata'))

		# HEAD request to detect resource type
		head_response = self.api.http_request('HEAD', uri)

		# 404, item does not exist, return False
		if head_response.status_code == 404:
			logger.debug('resource uri %s not found, returning False' % uri)
			return False

		# assume exists, parse headers for resource type and return instance
		elif head_response.status_code == 200:

			# parse LDP resource type from headers
			resource_type = self.api.parse_resource_type(head_response)
			logger.debug('using resource type: %s' % resource_type)

			# fire GET request
			get_response = self.api.http_request(
				'GET',
				"%s/fcr:metadata" % uri,
				response_format=response_format)

			# fire request
			return resource_type(self,
				uri,
				data=get_response.content,
				headers=get_response.headers,
				status_code=get_response.status_code)

		else:
			raise Exception('HTTP %s, error retrieving resource uri %s' % (head_response.status_code, uri))


	def start_txn(self, txn_name=None):

		'''
		Request new transaction from repository, init new Transaction, 
		store in self.txns

		Args:
			txn_name (str): human name for transaction
		
		Return:
			(Transaction): returns intance of newly created transaction
		'''

		# if no name provided, create one
		if not txn_name:
			txn_name = uuid.uuid4().hex

		# request new transaction
		txn_response = self.api.http_request('POST','%s/fcr:tx' % self.root, data=None, headers=None)

		# if 201, transaction was created
		if txn_response.status_code == 201:

			txn_uri = txn_response.headers['Location']
			logger.debug("spawning transaction: %s" % txn_uri)
			
			# init new Transaction, and pass Expires header
			txn = Transaction(
				self, # pass the repository
				txn_name,
				txn_uri,
				expires = txn_response.headers['Expires'])
				
			# append to self
			self.txns[txn_name] = txn

			# return 
			return txn


	def get_txn(self, txn_name, txn_uri):

		'''
		Retrieves known transaction and adds to self.txns.

		TODO:
			Perhaps this should send a keep-alive request as well?  Obviously still needed, and would reset timer.

		Args:
			txn_prefix (str, rdflib.term.URIRef): uri of the transaction. e.g. http://localhost:8080/rest/txn:123456789
			txn_name (str): local, human name for transaction
		
		Return:
			(Transaction) local instance of transactions from self.txns[txn_uri]
		'''

		# parse uri
		txn_uri = self.parse_uri(txn_uri)

		# request new transaction
		txn_response = self.api.http_request('GET',txn_uri, data=None, headers=None)

		# if 200, transaction exists
		if txn_response.status_code == 200:
			logger.debug("transactoin found: %s" % txn_uri)

			# init new Transaction, and pass Expires header
			txn = Transaction(
				self, # pass the repository
				txn_name,
				txn_uri,
				expires = None)
				
			# append to self
			self.txns[txn_name] = txn

			# return 
			return txn

		# if 404, transaction does not exist
		elif txn_response.status_code in [404, 410]:
			logger.debug("transaction does not exist: %s" % txn_uri)
			return False

		else:
			raise Exception('HTTP %s, could not retrieve transaction' % txn_response.status_code)



class Transaction(Repository):

	'''
	Class to represent open transactions.  Spawned by repository instance, these are stored in
	repo.txns.

	Inherits:
		Repository

	Args:
		txn_name (str): human name for transaction
		txn_uri (rdflib.term.URIRef, str): URI of transaction, also to be used as Transaction root path
		expires (str): expires information from headers
	'''

	def __init__(self, 
			repo,
			txn_name,
			txn_uri,
			expires = None
		):

		# fire parent Repository init()
		super().__init__(
			txn_uri,
			repo.username,
			repo.password,
			context = repo.context,
			default_serialization = repo.default_serialization)

		# Transaction init
		self.name = txn_name
		self.expires = expires

		# txn status
		self.active = True


	def keep_alive(self):

		'''
		Keep current transaction alive, updates self.expires

		Args:
			None

		Return:
			None: sets new self.expires
		'''

		# keep transaction alive
		txn_response = self.api.http_request('POST','%sfcr:tx' % self.root, data=None, headers=None)

		# if 204, transaction kept alive
		if txn_response.status_code == 204:
			logger.debug("continuing transaction: %s" % self.root)
			# update status and timer
			self.active = True
			self.expires = txn_response.headers['Expires']
			return  True

		# if 410, transaction does not exist
		elif txn_response.status_code == 410:
			logger.debug("transaction does not exist: %s" % self.root)
			self.active = False
			return False

		else:
			raise Exception('HTTP %s, could not continue transaction' % txn_response.status_code)


	def _close(self, close_type):

		'''
		Ends transaction by committing, or rolling back, all changes during transaction.

		Args:
			close_type (str): expects "commit" or "rollback"
		
		Return:
			(bool)
		'''

		# commit transaction
		txn_response = self.api.http_request('POST','%sfcr:tx/fcr:%s' % (self.root, close_type), data=None, headers=None)

		# if 204, transaction was closed
		if txn_response.status_code == 204:
			logger.debug("%s for transaction: %s, successful" % (close_type, self.root))
			# update self.active
			self.active = False
			# return
			return True

		# if 410 or 404, transaction does not exist
		elif txn_response.status_code in [404, 410]:
			logger.debug("transaction does not exist: %s" % self.root)
			# update self.active
			self.active = False
			return False

		else:
			raise Exception('HTTP %s, could not commit transaction' % txn_response.status_code)


	def commit(self):

		'''
		Fire self._close() method

		Args:
			None
		Returns:
			bool
		'''

		# fire _close method
		return self._close('commit')


	def rollback(self):

		'''
		Fire self._close() method

		Args:
			None
		Returns:
			bool
		'''

		# fire _close method
		return self._close('rollback')



class API(object):

	'''
	API for making requests and parsing responses from repository endpoint

	Args:
		repo (Repository): instance of Repository class
	'''

	def __init__(self, repo):

		# repository instance
		self.repo = repo


	def http_request(self, 
			verb,
			uri,
			data=None,
			headers=None,
			files=None,
			response_format=None,
			is_rdf = True,
			stream = False
		):

		'''
		Primary route for all HTTP requests to repository.  Ability to set most parameters for requests library,
		with some additional convenience parameters as well.

		Args:
			verb (str): HTTP verb to use for request, e.g. PUT, POST, GET, HEAD, PATCH, etc.
			uri (rdflib.term.URIRef,str): input URI
			data (str,file): payload of data to send for request, may be overridden in preperation of request
			headers (dict): optional dictionary of headers passed directly to requests.request
			files (dict): optional dictionary of files passed directly to requests.request
			response_format (str): desired response format for resource's payload, e.g. 'application/rdf+xml', 'text/turtle', etc.
			is_rdf (bool): if True, set Accept header based on combination of response_format and headers
			stream (bool): passed directly to requests.request for stream parameter

		Returns:
			requests.models.Response
		'''

		# set content negotiated response format for RDFSources
		if is_rdf:
			'''
			Acceptable content negotiated response formats include:
				application/ld+json (discouraged, if not prohibited, as it drops prefixes used in repository)
				application/n-triples
				application/rdf+xml
				text/n3 (or text/rdf+n3)
				text/plain
				text/turtle (or application/x-turtle)
			'''
			# set for GET requests only
			if verb == 'GET':
				# if no response_format has been requested to this point, use repository instance default
				if not response_format:
					response_format = self.repo.default_serialization
				# if headers present, append
				if headers and 'Accept' not in headers.keys():
					headers['Accept'] = response_format
				# if headers are blank, init dictionary
				else:
					headers = {'Accept':response_format}

		# prepare uri for HTTP request
		if type(uri) == rdflib.term.URIRef:
			uri = uri.toPython()

		logger.debug("%s request for %s, format %s, headers %s" % 
			(verb, uri, response_format, headers))

		# manually prepare request
		session = requests.Session()
		request = requests.Request(verb, uri, data=data, headers=headers, files=files)
		prepped_request = session.prepare_request(request)
		response = session.send(prepped_request,
			stream=stream,
		)
		return response


	def parse_resource_type(self, response):

		'''
		parse resource type from self.http_request()

		Args:
			response (requests.models.Response): response object

		Returns:
			[NonRDFSource, BasicContainer, DirectContainer, IndirectContainer]
		'''
		
		# parse 'Link' header
		links = [
			link.split(";")[0]
			for link in response.headers['Link'].split(', ')
			if link.startswith('<http://www.w3.org/ns/ldp#')]
		logger.debug('parsed Link headers: %s' % links)
		
		# with LDP types in hand, select appropriate resource type
		# NonRDF Source
		if '<http://www.w3.org/ns/ldp#NonRDFSource>' in links:
			return NonRDFSource
		# Basic Container
		elif '<http://www.w3.org/ns/ldp#BasicContainer>' in links:
			return BasicContainer
		# Direct Container
		elif '<http://www.w3.org/ns/ldp#DirectContainer>' in links:
			return DirectContainer
		# Indirect Container
		elif '<http://www.w3.org/ns/ldp#IndirectContainer>' in links:
			return IndirectContainer
		else:
			logger.debug('could not determine resource type from Link header, returning False')
			return False


	def parse_rdf_payload(self, data, headers):
	
		'''
		small function to parse RDF payloads from various repository endpoints

		Args:
			data (response.data): data from requests response
			headers (response.headers): headers from requests response

		Returns:
			(rdflib.Graph): parsed graph
		'''

		# handle edge case for content-types not recognized by rdflib parser
		if headers['Content-Type'].startswith('text/plain'):
			logger.debug('text/plain Content-Type detected, using application/n-triples for parser')
			parse_format = 'application/n-triples'
		else:
			parse_format = headers['Content-Type']

		# clean parse format for rdf parser (see: https://www.w3.org/2008/01/rdf-media-types)
		if ';charset' in parse_format:
			parse_format = parse_format.split(';')[0]
		
		# parse graph	
		graph = rdflib.Graph().parse(
			data=data.decode('utf-8'),
			format=parse_format)

		# return graph
		return graph



class SparqlUpdate(object):

	'''
	Class to handle the creation of Sparql updates via PATCH request.
	Accepts prefixes and graphs from resource, computes diff of graphs, and builds sparql query for update.

	Args:
		prefixes (types.SimpleNamespace): prefixes from resource at self.rdf.prefixes
		diffs (types.SimpleNamespace): diffs is comprised of three graphs that are derived from self._diff_graph(), at self.rdf.diffs 
	'''

	def __init__(self, prefixes, diffs):

		self.prefixes = prefixes
		self.diffs = diffs

		# prefixes and namespaces
		self.update_namespaces = set()
		self.update_prefixes = {}

	
	def _derive_namespaces(self):

		'''
		Small method to loop through three graphs in self.diffs, identify unique namespace URIs.
		Then, loop through provided dictionary of prefixes and pin one to another.

		Args:
			None: uses self.prefixes and self.diffs

		Returns:
			None: sets self.update_namespaces and self.update_prefixes
		'''

		# iterate through graphs and get unique namespace uris
		for graph in [self.diffs.overlap, self.diffs.removed, self.diffs.added]:
			for s,p,o in graph:
				ns_prefix, ns_uri, predicate = graph.compute_qname(p)
				self.update_namespaces.add(ns_uri)
		logger.debug(self.update_namespaces)

		# build unique prefixes dictionary
		# NOTE: can improve by using self.rdf.uris (reverse lookup of self.rdf.prefixes)
		for ns_uri in self.update_namespaces:
			for k in self.prefixes.__dict__:
				if str(ns_uri) == str(self.prefixes.__dict__[k]):
					logger.debug('adding prefix %s for uri %s to unique_prefixes' % (k,str(ns_uri)))
					self.update_prefixes[k] = self.prefixes.__dict__[k]


	def build_query(self):

		'''
		Using the three graphs derived from self._diff_graph(), build a sparql update query in the format:
		
		PREFIX foo: <http://foo.com>
		PREFIX bar: <http://bar.com>

		DELETE {...}
		INSERT {...}
		WHERE {...}

		Args:
			None: uses variables from self

		Returns:
			(str) sparql update query as string

		'''

		# derive namespaces to include prefixes in Sparql update query
		self._derive_namespaces()

		sparql_query = ''

		# add prefixes
		for ns_prefix, ns_uri in self.update_prefixes.items():
			sparql_query += "PREFIX %s: <%s>\n" % (ns_prefix, str(ns_uri))

		# deletes
		removed_serialized = self.diffs.removed.serialize(format='nt').decode('utf-8')
		sparql_query += '\nDELETE {\n%s}\n\n' % removed_serialized

		# inserts
		added_serialized = self.diffs.added.serialize(format='nt').decode('utf-8')
		sparql_query += '\nINSERT {\n%s}\n\n' % added_serialized

		# where (not yet implemented)
		sparql_query += 'WHERE {}'

		# debug
		# logger.debug(sparql_query)

		# return query
		return sparql_query



class Resource(object):

	'''
	Linked Data Platform Resource (LDPR)
	A HTTP resource whose state is represented in any way that conforms to the simple lifecycle patterns and conventions in section 4. Linked Data Platform Resources.
	https://www.w3.org/TR/ldp/

	In the LDP hierarchy, this class represents the most abstract entity of "Resource".

	Sub-classed by:
		NonRDFSource, Container

	Args:
		repo (Repository): instance of Repository class
		uri (rdflib.term.URIRef,str): input URI
		data (): passed from sub-classes
		headers (dict): passed from sub-classes
		status_code (int): passed from sub-classes
		rdf_prefixes_mixins (dict): optional rdf prefixes and namespaces
	'''
	
	def __init__(self,
		repo,
		uri=None,
		data=None,
		headers={},
		status_code=None,
		rdf_prefixes_mixins=None):

		# repository handle is pinned to resource instance here
		self.repo = repo

		# parse uri with parse_uri() from repo instance
		self.uri = self.repo.parse_uri(uri)
		
		# HTTP
		self.headers = headers
		self.status_code = status_code
		# if status_code provided, and 200, set exists attribute as True
		if self.status_code == 200:
			self.exists = True
		else:
			self.exists = False

		# RDF
		self._build_rdf(data=data)

		# versions
		self.versions = SimpleNamespace()


	def __repr__(self):
		return '<%s Resource, uri: %s>' % (self.__class__.__name__, self.uri)


	def uri_as_string(self):

		'''
		return rdflib.term.URIRef URI as string

		Returns:
			(str)
		'''

		return self.uri.toPython()


	def check_exists(self):
		
		'''
		Check if resource exists, update self.exists, returns

		Returns:
			None: sets self.exists
		'''

		response = self.repo.api.http_request('HEAD', self.uri)
		self.status_code = response.status_code
		# resource exists
		if self.status_code == 200:
			self.exists = True
		# resource no longer here
		elif self.status_code == 410:
			self.exists = False
		# resource not found
		elif self.status_code == 404:
			self.exists = False
		return self.exists


	def create(self, specify_uri=False, ignore_tombstone=False, serialization_format=None, stream=False, refresh=True):

		'''
		Primary method to create resources.

		Args:
			specify_uri (bool): If True, uses PUT verb and sets the URI during creation.  If False, uses POST and gets repository minted URI
			ignore_tombstone (bool): If True, will attempt creation, if tombstone exists (409), will delete tombstone and retry
			serialization_format(str): Content-Type header / mimetype that will be used to serialize self.rdf.graph, and set headers for PUT/POST requests
		'''

		# if resource claims existence, raise exception
		if self.exists:
			raise Exception('resource exists attribute True, aborting')

		# else, continue
		else:

			# determine verb based on specify_uri parameter
			if specify_uri:
				verb = 'PUT'
			else:
				verb = 'POST'
			
			logger.debug('creating resource %s with verb %s' % (self.uri, verb))

			# check if NonRDFSource, if so, run _prep_binary_data() and set data to self.binary.data
			if type(self) == NonRDFSource:
				self._prep_binary_data()
				data = self.binary.data

			# otherwise, prep for RDF
			else:
				# determine serialization
				if not serialization_format:
					serialization_format = self.repo.default_serialization
				data = self.rdf.graph.serialize(format=serialization_format)
				self.headers['Content-Type'] = serialization_format
			
			# fire creation request
			response = self.repo.api.http_request(verb, self.uri, data=data, headers=self.headers, stream=stream)
			return self._handle_create(response, ignore_tombstone, refresh)
			

	def _handle_create(self, response, ignore_tombstone, refresh):

		'''
		Handles response from self.create()

		Args:
			response (requests.models.Response): response object from self.create()
			ignore_tombstone (bool): If True, will attempt creation, if tombstone exists (409), will delete tombstone and retry
		'''

		# 201, success, refresh
		if response.status_code == 201:
			# if not specifying uri, capture from response and append to object
			self.uri = self.repo.parse_uri(response.text)
			# creation successful, update resource
			if refresh:
				self.refresh()

		# 404, assumed POST, target location does not exist
		elif response.status_code == 404:
			raise Exception('HTTP 404, for this POST request target location does not exist')

		# 409, conflict, resource likely exists
		elif response.status_code == 409:
			raise Exception('HTTP 409, resource already exists')
		
		# 410, tombstone present
		elif response.status_code == 410:
			if ignore_tombstone:
				response = self.repo.api.http_request('DELETE', '%s/fcr:tombstone' % self.uri)
				if response.status_code == 204:
					logger.debug('tombstone removed, retrying create')
					self.create()
				else:
					raise Exception('HTTP %s, Could not remove tombstone for %s' % (response.status_code, self.uri))
			else:
				raise Exception('tombstone for %s detected, aborting' % self.uri)

		# 415, unsupported media type
		elif response.status_code == 415:
			raise Exception('HTTP 415, unsupported media type')

		# unknown status code
		else:
			raise Exception('HTTP %s, unknown error creating resource' % response.status_code)

		# if all goes well, return True
		return True


	def options(self):

		'''
		Small method to return headers of an OPTIONS request to self.uri

		Args:
			None

		Return:
			(dict) response headers from OPTIONS request
		'''

		# http request
		response = self.repo.api.http_request('OPTIONS', self.uri)
		return response.headers


	def move(self, destination, remove_tombstone=True):

		'''
		Method to move resource to another location.
		Note: by default, this leaves a tombstone.  Can use optional flag remove_tombstone to remove on successful move.

		Args:
			destination (rdflib.term.URIRef, str): URI location to move resource

		Returns:
			(Resource) new, moved instance of resource
		'''

		# set move headers
		destination_uri = self.repo.parse_uri(destination)

		# http request
		response = self.repo.api.http_request('MOVE', self.uri, data=None, headers={'Destination':destination_uri.toPython()})

		# handle response
		if response.status_code == 201:
			# set self exists
			self.exists = False
			# handle tombstone
			if remove_tombstone:
				tombstone_response = self.repo.api.http_request('DELETE', "%s/fcr:tombstone" % self.uri)
			return destination_uri
		else:
			raise Exception('HTTP %s, could not move resource %s to %s' % (response.status_code, self.uri, destination_uri))


	def copy(self, destination):

		'''
		Method to copy resource to another location

		Args:
			destination (rdflib.term.URIRef, str): URI location to move resource

		Returns:
			(Resource) new, moved instance of resource
		'''

		# set move headers
		destination_uri = self.repo.parse_uri(destination)

		# http request
		response = self.repo.api.http_request('COPY', self.uri, data=None, headers={'Destination':destination_uri.toPython()})

		# handle response
		if response.status_code == 201:
			return destination_uri
		else:
			raise Exception('HTTP %s, could not move resource %s to %s' % (response.status_code, self.uri, destination_uri))


	def delete(self, remove_tombstone=True):

		'''
		Method to delete resources.

		Args:
			remove_tombstone (bool): If True, will remove tombstone at uri/fcr:tombstone when removing resource.

		Returns:
			(bool)
		'''

		response = self.repo.api.http_request('DELETE', self.uri)

		# update exists
		if response.status_code == 204:
			# removal successful, updating self
			self._empty_resource_attributes()

		if remove_tombstone:
			self.repo.api.http_request('DELETE', '%s/fcr:tombstone' % self.uri)

		return True


	def refresh(self):
		
		'''
		Performs GET request and refreshes RDF information for resource.

		Args:
			None

		Returns:
			None
		'''

		updated_self = self.repo.get_resource(self.uri)

		# if resource type of updated_self != self, raise exception
		if type(updated_self) != type(self):
			raise Exception('Instantiated %s, but repository reports this resource is %s' % (type(updated_self), type(self)) )

		if updated_self:
			# update attributes
			self.status_code = updated_self.status_code
			self.rdf.data = updated_self.rdf.data
			self.headers = updated_self.headers
			self.exists = updated_self.exists
			# update graph if RDFSource
			if type(self) != NonRDFSource:
				self._parse_graph()
			# empty versions
			self.versions = SimpleNamespace()
			# cleanup
			del(updated_self)
		else:
			logger.debug('resource %s not found, dumping values')
			self._empty_resource_attributes()
			

	def _build_rdf(self, data=None):

		'''
		Parse incoming rdf as self.rdf.orig_graph, create copy at self.rdf.graph

		Args:
			data (): payload from GET request, expected RDF content in various serialization formats

		Returns:
			None
		'''

		# recreate rdf data
		self.rdf = SimpleNamespace()
		self.rdf.data = data
		self.rdf.prefixes = SimpleNamespace()
		self.rdf.uris = SimpleNamespace()
		# populate prefixes
		for prefix,uri in self.repo.context.items():
			setattr(self.rdf.prefixes, prefix, rdflib.Namespace(uri))
		# graph
		self._parse_graph()


	def _parse_graph(self):

		'''
		use Content-Type from headers to determine parsing method

		Args:
			None

		Return:
			None: sets self.rdf by parsing data from GET request, or setting blank graph of resource does not yet exist
		'''

		# if resource exists, parse self.rdf.data
		if self.exists:
			self.rdf.graph = self.repo.api.parse_rdf_payload(self.rdf.data, self.headers)

		# else, create empty graph
		else:
			self.rdf.graph = rdflib.Graph()
			
		# bind any additional namespaces from repo instance, but do not override
		self.rdf.namespace_manager = rdflib.namespace.NamespaceManager(self.rdf.graph)
		for ns_prefix, ns_uri in self.rdf.prefixes.__dict__.items():
			self.rdf.namespace_manager.bind(ns_prefix, ns_uri, override=False)

		# conversely, add namespaces from parsed graph to self.rdf.prefixes
		for ns_prefix, ns_uri in self.rdf.graph.namespaces():
			setattr(self.rdf.prefixes, ns_prefix, rdflib.Namespace(ns_uri))
			setattr(self.rdf.uris, rdflib.Namespace(ns_uri), ns_prefix)

		# pin old graph to resource, create copy graph for modifications
		self.rdf._orig_graph = copy.deepcopy(self.rdf.graph)

		# parse triples for object-like access
		self.parse_object_like_triples()


	def parse_object_like_triples(self):

		'''
		method to parse triples from self.rdf.graph for object-like 
		access

		Args:
			None

		Returns:
			None: sets self.rdf.triples
		'''

		# parse triples as object-like attributes in self.rdf.triples
		self.rdf.triples = SimpleNamespace() # prepare triples
		for s,p,o in self.rdf.graph:
			
			# get ns info
			ns_prefix, ns_uri, predicate = self.rdf.graph.compute_qname(p)
			
			# if prefix as list not yet added, add
			if not hasattr(self.rdf.triples, ns_prefix):
				setattr(self.rdf.triples, ns_prefix, SimpleNamespace())

			# same for predicate
			if not hasattr(getattr(self.rdf.triples, ns_prefix), predicate):
				setattr(getattr(self.rdf.triples, ns_prefix), predicate, [])			

			# append object for this prefix
			getattr(getattr(self.rdf.triples, ns_prefix), predicate).append(o)


	def _diff_graph(self):

		'''
		Uses rdflib.compare diff, https://github.com/RDFLib/rdflib/blob/master/rdflib/compare.py
		When a resource is retrieved, the graph retrieved and parsed at that time is saved to self.rdf._orig_graph,
		and all local modifications are made to self.rdf.graph.  This method compares the two graphs and returns the diff
		in the format of three graphs:

			overlap - triples shared by both
			removed - triples that exist ONLY in the original graph, self.rdf._orig_graph
			added - triples that exist ONLY in the modified graph, self.rdf.graph

		These are used for building a sparql update query for self.update.

		Args:
			None
		
		Returns:
			None: sets self.rdf.diffs and adds the three graphs mentioned, 'overlap', 'removed', and 'added'
		'''

		overlap, removed, added = graph_diff(
			to_isomorphic(self.rdf._orig_graph),
			to_isomorphic(self.rdf.graph))
		diffs = SimpleNamespace()
		diffs.overlap = overlap
		diffs.removed = removed
		diffs.added = added
		self.rdf.diffs = diffs


	def add_namespace(self, ns_prefix, ns_uri):

		'''
		preferred method is to instantiate with repository under 'context',
		but prefixes / namespaces can be added for a Resource instance

		adds to self.rdf.prefixes which will endure through create/update/refresh,
		and get added back to parsed graph namespaces

		Args:
			ns_prefix (str): prefix for namespace, e.g. 'dc', 'foaf'
			ns_uri (str): string of namespace / ontology. e.g. 'http://purl.org/dc/elements/1.1/', 'http://xmlns.com/foaf/0.1/'

		Returns:
			None: binds this new prefix:namespace combination to self.rdf.prefixes for use, and self.rdf.graph for serialization
		'''

		# add to prefixes
		setattr(self.rdf.prefixes, ns_prefix, rdflib.Namespace(ns_uri))

		# bind to graph
		self.rdf.namespace_manager.bind(ns_prefix, ns_uri, override=False)


	def _empty_resource_attributes(self):

		'''
		small method to empty values if resource is removed or absent

		Args:
			None

		Return:
			None: empties selected resource attributes
		'''

		self.status_code = 404
		self.headers = {}
		self.exists = False

		# build RDF
		self.rdf = self._build_rdf()

		# if NonRDF recreate binary data
		if type(self) == NonRDFSource:
			self._build_binary()


	def _handle_object(self, object_input):

		'''
		Method to handle possible values passed for adding, removing, modifying triples.
		Detects type of input and sets appropriate http://www.w3.org/2001/XMLSchema# datatype

		Args:
			object_input (str,int,datetime,): many possible inputs

		Returns:
			(rdflib.term.Literal): with appropriate datatype attribute
		'''

		# if object is string, convert to rdflib.term.Literal with appropriate datatype
		if type(object_input) == str:
			return rdflib.term.Literal(object_input, datatype=rdflib.XSD.string)

		# integer
		elif type(object_input) == int:
			return rdflib.term.Literal(object_input, datatype=rdflib.XSD.int)

		# float
		elif type(object_input) == float:
			return rdflib.term.Literal(object_input, datatype=rdflib.XSD.float)

		# date
		elif type(object_input) == datetime.datetime:
			return rdflib.term.Literal(object_input, datatype=rdflib.XSD.date)

		else:
			return object_input


	def add_triple(self, p, o, refresh_quick_triples=True):

		'''
		add triple by providing p,o, assumes s = subject

		Args:
			p (rdflib.term.URIRef): predicate
			o (): object
			refresh_quick_triples (bool): whether or not to update object-like self.rdf.triples

		Returns:
			None: adds triple to self.rdf.graph
		'''

		self.rdf.graph.add((self.uri, p, self._handle_object(o)))

		# refresh self.rdf.triples
		if refresh_quick_triples:
			self.parse_object_like_triples()


	def set_triple(self, p, o, refresh_quick_triples=True):
		
		'''
		Assuming the predicate or object matches a single triple, sets the other for that triple.

		Args:
			p (rdflib.term.URIRef): predicate
			o (): object
			refresh_quick_triples (bool): whether or not to update object-like self.rdf.triples

		Returns:
			None: modifies pre-existing triple in self.rdf.graph
		'''
		
		self.rdf.graph.set((self.uri, p, self._handle_object(o)))

		# refresh self.rdf.triples
		if refresh_quick_triples:
			self.parse_object_like_triples()


	def remove_triple(self, p, o, refresh_quick_triples=True):

		'''
		remove triple by supplying p,o

		Args:
			p (rdflib.term.URIRef): predicate
			o (): object
			refresh_quick_triples (bool): whether or not to update object-like self.rdf.triples

		Returns:
			None: removes triple from self.rdf.graph
		'''

		self.rdf.graph.remove((self.uri, p, self._handle_object(o)))

		# refresh self.rdf.triples
		if refresh_quick_triples:
			self.parse_object_like_triples()


	# update RDF, and for NonRDFSource, binaries
	def update(self, sparql_query_only=False, refresh=True):

		'''
		Method to update resources in repository.  Firing this method computes the difference in the local modified graph and the original one, 
		creates an instance of SparqlUpdate and builds a sparql query that represents these differences, and sends this as a PATCH request.

		If the resource is NonRDF (Binary), this also method also updates the binary data.

		Args:
			sparql_query_only (bool): If True, returns only the sparql query string and does not perform any actual updates

		Returns:
			(bool) 
		'''

		# run diff on graphs, send as PATCH request
		self._diff_graph()		
		sq = SparqlUpdate(self.rdf.prefixes, self.rdf.diffs)
		if sparql_query_only:
			return sq.build_query()
		response = self.repo.api.http_request(
			'PATCH',
			self.uri,
			data=sq.build_query(),
			headers={'Content-Type':'application/sparql-update'})

		# if NonRDFSource, update binary as well
		if type(self) == NonRDFSource:
			self._prep_binary_data()
			binary_data = self.binary.data
			binary_response = self.repo.api.http_request(
				'PUT',
				self.uri,
				data=binary_data,
				headers={'Content-Type':self.binary.mimetype})

		# if status_code == 204, resource changed, refresh graph
		if response.status_code == 204:
			if refresh:
				self.refresh()
			return True


	def children(self, as_resources=False):

		'''
		method to return hierarchical  children of this resource

		Args:
			as_resources (bool): if True, opens each as appropriate resource type instead of return URI only

		Returns:
			(list): list of resources
		'''

		children = [o for s,p,o in self.rdf.graph.triples((None, self.rdf.prefixes.ldp.contains, None))]

		# if as_resources, issue GET requests for children and return
		if as_resources:
			logger.debug('retrieving children as resources')
			children = [ self.repo.get_resource(child) for child in children ]

		return children


	def parents(self, as_resources=False):

		'''
		method to return hierarchical parents of this resource

		Args:
			as_resources (bool): if True, opens each as appropriate resource type instead of return URI only

		Returns:
			(list): list of resources
		'''

		parents = [o for s,p,o in self.rdf.graph.triples((None, self.rdf.prefixes.fedora.hasParent, None))]

		# if as_resources, issue GET requests for children and return
		if as_resources:
			logger.debug('retrieving parent as resource')
			parents = [ self.repo.get_resource(parent) for parent in parents ]

		return parents


	def siblings(self, as_resources=False):

		'''
		method to return hierarchical siblings of this resource.

		Args:
			as_resources (bool): if True, opens each as appropriate resource type instead of return URI only

		Returns:
			(list): list of resources
		'''

		siblings = set()

		# loop through parents and get children
		for parent in self.parents(as_resources=True):
			for sibling in parent.children(as_resources=as_resources):				
				siblings.add(sibling)

		# remove self
		if as_resources:
			siblings.remove(self)
		if not as_resources:
			siblings.remove(self.uri)

		return list(siblings)


	def _affix_version(self, version_uri, version_label):

		# retrieve version
		version_resource = self.repo.get_resource(version_uri)

		# instantiate ResourceVersion
		rv = ResourceVersion(self, version_resource, version_uri, version_label)

		# append to self.versions
		setattr(self.versions, version_label, rv)


	def create_version(self, version_label):

		'''
		method to create a new version of the resource as it currently stands
			
			- Note: this will create a version based on the current live instance of the resource,
			not the local version, which might require self.update() to update.

		Args:
			version_label (str): label to be used for version

		Returns:
			(ResourceVersion): instance of ResourceVersion, also appended to self.versions
		'''

		# create version
		version_response = self.repo.api.http_request('POST', '%s/fcr:versions' % self.uri, data=None, headers={'Slug':version_label})

		# if 201, assume success
		if version_response.status_code == 201:
			logger.debug('version created: %s' % version_response.headers['Location'])

			# affix version
			self._affix_version(version_response.headers['Location'], version_label)


	def get_versions(self):

		'''
		retrieves all versions of an object, and stores them at self.versions

		Args:
			None

		Returns:
			None: appends instances 
		'''

		# get all versions
		versions_response = self.repo.api.http_request('GET', '%s/fcr:versions' % self.uri)

		# parse response
		versions_graph = self.repo.api.parse_rdf_payload(versions_response.content, versions_response.headers)

		# loop through fedora.hasVersion
		for version_uri in versions_graph.objects(self.uri, self.rdf.prefixes.fedora.hasVersion):

			# get label
			version_label = versions_graph.value(version_uri, self.rdf.prefixes.fedora.hasVersionLabel, None).toPython()

			# affix version
			self._affix_version(version_uri, version_label)



class ResourceVersion(Resource):

	'''
	Class to represent versions of a resource.

	Versions are spawned by the Resource class method resource.create_version(), or retrieved by resource.get_versions().
	Versions are stored in the resource instance at resource.versions

	Args:
		version_resource (Resource): retrieved and prased resource version
		version_uri (rdflib.term.URIRef, str): uri of version
		version_label (str): lable for version
	'''

	def __init__(self, current_resource, version_resource, version_uri, version_label):

		self._current_resource = current_resource
		self.resource = version_resource
		self.uri = version_uri
		self.label = version_label


	def revert_to(self):
		
		'''
		method to revert resource to this version by issuing PATCH

		Args:
			None

		Returns:
			None: sends PATCH request, and refreshes parent resource
		'''
		
		# send patch
		response = self.resource.repo.api.http_request('PATCH', self.uri)

		# if response 204
		if response.status_code == 204:
			logger.debug('reverting to previous version of resource, %s' % self.uri)

			# refresh current resource handle
			self._current_resource.refresh()

		else:
			raise Exception('HTTP %s, could not revert to resource version, %s' % (response.status_code, self.uri))


	def delete(self):

		'''
		method to remove version from resource's history
		'''
		
		# send patch
		response = self.resource.repo.api.http_request('DELETE', self.uri)

		# if response 204
		if response.status_code == 204:
			logger.debug('deleting previous version of resource, %s' % self.uri)

			# remove from resource versions
			delattr(self._current_resource.versions, self.label)

		# if 400, likely most recent version and cannot remove
		elif response.status_code == 400:
			raise Exception('HTTP 400, likely most recent resource version which cannot be removed')

		else:
			raise Exception('HTTP %s, could not delete resource version: %s' % (response.status_code, self.uri))



# NonRDF Source
class NonRDFSource(Resource):

	'''
	Linked Data Platform Non-RDF Source (LDP-NR)
	An LDPR whose state is not represented in RDF. For example, these can be binary or text documents that do not have useful RDF representations.
	https://www.w3.org/TR/ldp/

	Note: When a pre-existing NonRDFSource is retrieved, the binary data is stored under self.binary.data as a
	streamable requests object.

	Inherits:
		Resource

	Args:
		repo (Repository): instance of Repository class
		uri (rdflib.term.URIRef,str): input URI
		data (): passed from sub-classes
		headers (dict): passed from sub-classes
		status_code (int): passed from sub-classes
	'''
	
	def __init__(self, repo, uri=None, data=None, headers={}, status_code=None):

		self.mimetype = None

		# fire parent Resource init()
		super().__init__(repo, uri=uri, data=data, headers=headers, status_code=status_code)

		# binary data
		self._build_binary()
		
		# like RDF, if exists, retrieve binary data
		if self.exists:
			self.parse_binary()
			

	def parse_binary(self):

		'''
		when retrieving a NonRDF resource, parse binary data and make available
		via generators
		'''

		# derive mimetype
		self.binary.mimetype = self.rdf.graph.value(
			self.uri,
			self.rdf.prefixes.ebucore.hasMimeType).toPython()
		
		# get binary content as stremable response
		self.binary.data = self.repo.api.http_request(
			'GET',
			self.uri,
			data=None,
			headers={'Content-Type':self.binary.mimetype},
			is_rdf=False,
			stream=True)


	def _build_binary(self):

		'''
		builds binary attributes for resource

		Args:
			None

		Return:
			None: sets various attributes
		'''

		# binary data
		self.binary = SimpleNamespace()
		self.binary.delivery = None
		self.binary.data = None
		self.binary.stream = False
		self.binary.mimetype = None # convenience attribute that is written to headers['Content-Type'] for create/update
		self.binary.location = None
		self.binary.range = self.byte_range


	def _prep_binary_data(self):

		'''
		method is used to check/prep data and headers for NonRDFSource create or update

		Args:
			None

		Returns:
			None: sets attributes in self.binary and headers
		'''

		logger.debug('preparing NonRDFSource data for create/update')

		# handle mimetype / Content-Type
		self._prep_binary_mimetype()

		# handle binary data
		self._prep_binary_content()
		

	def _prep_binary_mimetype(self):

		'''
		Sets Content-Type header based on headers and/or self.binary.mimetype values
		Implicitly favors Content-Type header if set

		Args:
			None

		Returns:
			None: sets attributes in self.binary and headers
		'''

		# neither present
		if not self.binary.mimetype and 'Content-Type' not in self.headers.keys():
			raise Exception('to create/update NonRDFSource, mimetype or Content-Type header is required')
		
		# mimetype, no Content-Type
		elif self.binary.mimetype and 'Content-Type' not in self.headers.keys():
			logger.debug('setting Content-Type header with provided mimetype: %s'
				% self.binary.mimetype)
			self.headers['Content-Type'] = self.binary.mimetype


	def _prep_binary_content(self):

		'''		
		Sets delivery method of either payload or header
		Favors Content-Location header if set

		Args:
			None

		Returns:
			None: sets attributes in self.binary and headers
		'''

		# nothing present
		if not self.binary.data and not self.binary.location and 'Content-Location' not in self.headers.keys():
			raise Exception('creating/updating NonRDFSource requires content from self.binary.data, self.binary.location, or the Content-Location header')

		elif 'Content-Location' in self.headers.keys():
			logger.debug('Content-Location header found, using')
			self.binary.delivery = 'header'
		
		# if Content-Location is not set, look for self.data_location then self.data
		elif 'Content-Location' not in self.headers.keys():

			# data_location set, trumps Content self.data
			if self.binary.location:
				# set appropriate header
				self.headers['Content-Location'] = self.binary.location
				self.binary.delivery = 'header'

			# data attribute is plain text, binary, or file-like object
			elif self.binary.data:

				# if file-like object, set flag for api.http_request
				if isinstance(self.binary.data, io.BufferedIOBase):
					logger.debug('detected file-like object')
					self.binary.delivery = 'payload'

				# else, just bytes
				else:
					logger.debug('detected bytes')
					self.binary.delivery = 'payload'


	def fixity(self, response_format=None):

		'''
		Issues fixity check, return parsed graph

		Args:
			None

		Returns:
			(dict): ('verdict':(bool): verdict of fixity check, 'premis_graph':(rdflib.Graph): parsed PREMIS graph from check)
		'''

		# if no response_format, use default
		if not response_format:
			response_format = self.repo.default_serialization

		# issue GET request for fixity check
		response = self.repo.api.http_request('GET', '%s/fcr:fixity' % self.uri)

		# parse
		fixity_graph = self.repo.api.parse_rdf_payload(response.content, response.headers)

		# determine verdict		
		for outcome in fixity_graph.objects(None, self.rdf.prefixes.premis.hasEventOutcome):
			if outcome.toPython() == 'SUCCESS':
				verdict = True
			else:
				verdict = False

		return {
			'verdict':verdict,
			'premis_graph':fixity_graph
		}


	def byte_range(self, byte_start, byte_end, stream=False):

		'''
		method to return a particular byte range from NonRDF resource's binary data
		https://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html

		Args:
			byte_start(int): position of range start 
			byte_end(int): position of range end

		Returns:
			(requests.Response): streamable response
		'''

		response = self.repo.api.http_request(
			'GET',
			self.uri,
			data=None,
			headers={
				'Content-Type':self.binary.mimetype,
				'Range':'bytes=%s-%s' % (byte_start, byte_end)
			},
			is_rdf=False,
			stream=stream)

		return response


# 'Binary' is an alias for NonRDFSource
Binary = NonRDFSource


# RDF Source
class RDFResource(Resource):

	'''
	Linked Data Platform RDF Source (LDP-RS)
	An LDPR whose state is fully represented in RDF, corresponding to an RDF graph. See also the term RDF Source from [rdf11-concepts].
	https://www.w3.org/TR/ldp/

	Sub-classed by:
		Container

	Inherits:
		Resource

	Args:
		repo (Repository): instance of Repository class
		uri (rdflib.term.URIRef,str): input URI
		data (): passed from sub-classes
		headers (dict): passed from sub-classes
		status_code (int): passed from sub-classes
	'''
	
	def __init__(self, repo, uri=None, data=None, headers={}, status_code=None):
		
		# fire parent Resource init()
		super().__init__(repo, uri=uri, data=data, headers=headers, status_code=status_code)



# Container
class Container(RDFResource):
	
	'''
	Linked Data Platform Container (LDPC)
	A LDP-RS representing a collection of linked documents (RDF Document [rdf11-concepts] or information resources [WEBARCH]) that responds to client requests for creation, modification, and/or enumeration of its linked members and documents, and that conforms to the simple lifecycle patterns and conventions in section 5. Linked Data Platform Containers.
	https://www.w3.org/TR/ldp/

	Sub-classed by:
		BasicContainer, IndirectContainer, DirectContainer

	Inherits:
		RDFResource

	Args:
		repo (Repository): instance of Repository class
		uri (rdflib.term.URIRef,str): input URI
		data (): passed from sub-classes
		headers (dict): passed from sub-classes
		status_code (int): passed from sub-classes
	'''

	def __init__(self, repo, uri=None, data=None, headers={}, status_code=None):
		
		# fire parent RDFResource init()
		super().__init__(repo, uri=uri, data=data, headers=headers, status_code=status_code)



# Basic Container
class BasicContainer(Container):
	
	'''
	Linked Data Platform Basic Container (LDP-BC)
	An LDPC that defines a simple link to its contained documents (information resources) [WEBARCH].
	https://www.w3.org/TR/ldp/

	https://gist.github.com/hectorcorrea/dc20d743583488168703
		- "The important thing to notice is that by posting to a Basic Container, the LDP server automatically adds a triple with ldp:contains predicate pointing to the new resource created."

	Inherits:
		Container

	Args:
		repo (Repository): instance of Repository class
		uri (rdflib.term.URIRef,str): input URI
		data (): passed from sub-classes
		headers (dict): passed from sub-classes
		status_code (int): passed from sub-classes
	'''
	
	def __init__(self, repo, uri=None, data=None, headers={}, status_code=None):

		# fire parent Container init()
		super().__init__(repo, uri=uri, data=data, headers=headers, status_code=status_code)



# Direct Container
class DirectContainer(Container):
	
	'''
	Linked Data Platform Direct Container (LDP-DC)
	An LDPC that adds the concept of membership, allowing the flexibility of choosing what form its membership triples take, and allows members to be any resources [WEBARCH], not only documents.
	https://www.w3.org/TR/ldp/

	When adding children, can also write relationships to another resource

	Inherits:
		Container

	Args:
		repo (Repository): instance of Repository class
		uri (rdflib.term.URIRef,str): input URI
		data (): passed from sub-classes
		headers (dict): passed from sub-classes
		status_code (int): passed from sub-classes
		membershipResource (rdflib.term.URIRef): resource that will accumlate triples as children are added
		hasMemberRelation (rdflib.term.URIRef): predicate that will be used when pointing from URI in ldp:membershipResource to children
	'''
	
	def __init__(self,
		repo,
		uri=None,
		data=None,
		headers={},
		status_code=None,
		membershipResource=None,
		hasMemberRelation=None):

		# fire parent Container init()
		super().__init__(repo, uri=uri, data=data, headers=headers, status_code=status_code)

		# if resource does not yet exist, set rdf:type
		self.add_triple(self.rdf.prefixes.rdf.type, self.rdf.prefixes.ldp.DirectContainer)

		# if membershipResource or hasMemberRelation args are set, set triples
		if membershipResource:
			self.add_triple(self.rdf.prefixes.ldp.membershipResource, membershipResource)
		if hasMemberRelation:
			self.add_triple(self.rdf.prefixes.ldp.hasMemberRelation, hasMemberRelation)



# Indirect Container
class IndirectContainer(Container):
	
	'''
	Linked Data Platform Indirect Container (LDP-IC)
	An LDPC similar to a LDP-DC that is also capable of having members whose URIs are based on the content of its contained documents rather than the URIs assigned to those documents.
	https://www.w3.org/TR/ldp/

	Inherits:
		Container

	Args:
		repo (Repository): instance of Repository class
		uri (rdflib.term.URIRef,str): input URI
		data (): passed from sub-classes
		headers (dict): passed from sub-classes
		status_code (int): passed from sub-classes
		membershipResource (rdflib.term): resource that will accumlate triples as children are added
		hasMemberRelation (rdflib.term): predicate that will be used when pointing from URI in ldp:membershipResource to ldp:insertedContentRelation
		insertedContentRelation (rdflib.term): destination for ldp:hasMemberRelation from ldp:membershipResource
	'''

	def __init__(self,
		repo,
		uri=None,
		data=None,
		headers={},
		status_code=None,
		membershipResource=None,
		hasMemberRelation=None,
		insertedContentRelation=None):

		# fire parent Container init()
		super().__init__(repo, uri=uri, data=data, headers=headers, status_code=status_code)
	
		# if resource does not yet exist, set rdf:type
		self.add_triple(self.rdf.prefixes.rdf.type, self.rdf.prefixes.ldp.IndirectContainer)

		# if membershipResource, hasMemberRelation, or insertedContentRelation args are set, set triples
		if membershipResource:
			self.add_triple(self.rdf.prefixes.ldp.membershipResource, membershipResource)
		if hasMemberRelation:
			self.add_triple(self.rdf.prefixes.ldp.hasMemberRelation, hasMemberRelation)
		if insertedContentRelation:
			self.add_triple(self.rdf.prefixes.ldp.insertedContentRelation, insertedContentRelation)


