# ******************************************************
# Copyright © 2020-2021 VMware, Inc. All rights reserved.
# ******************************************************

"""
Description : Module performs VMware Cloud Director validations related for NSX-V To NSX-T
"""

import inspect
from functools import wraps
from collections import OrderedDict, defaultdict
import copy
import json
import logging
import os
import re
import threading
import time
import traceback

import ipaddress
import requests
import xmltodict

import src.constants as mainConstants
import src.core.vcd.vcdConstants as vcdConstants

from src.commonUtils.restClient import RestAPIClient
from src.commonUtils.threadUtils import Thread
from src.commonUtils.utils import Utilities

METADATA_SAVE_FALSE = False

logger = logging.getLogger('mainLogger')

def getSession(self):
    if hasattr(self, '__threadname__') and self.__threadname__:
        threading.current_thread().name = self.__threadname__
    threading.current_thread().name = self.vdcName
    url = '{}session'.format(vcdConstants.XML_API_URL.format(self.ipAddress))
    response = self.restClientObj.get(url, headers=self.headers)
    if response.status_code != requests.codes.ok:
        logger.debug('Session expired!. Re-login to the vCloud Director')
        self.vcdLogin()


def isSessionExpired(func):
    """
        Description : decorator to check and get vcd Rest API session
    """
    @wraps(func)
    def inner(self, *args, **kwargs):
        getSession(self)
        result = func(self, *args, **kwargs)
        return result
    return inner


def remediate(func):
    """
        Description : decorator to save task status and save metadata in Org VDC after task is performed successfully
    """
    @wraps(func)
    def inner(self, *args, **kwargs):
        if not self.rollback.retryRollback and (self.rollback.metadata.get(func.__name__) or
                                                self.rollback.metadata.get(inspect.stack()[2].function,
                                                                           {}).get(func.__name__)):
            return

        # Getting vcd rest api session
        getSession(self)

        if self.rollback.metadata and not hasattr(self.rollback, 'retry') and not self.rollback.retryRollback:
            logger.info('Continuing migration of NSX-V backed Org VDC to NSX-T backed from {}.'.format(self.__desc__))
            self.rollback.retry = True

        if inspect.stack()[2].function != 'run' and inspect.stack()[2].function != '<module>':
            if not self.rollback.executionResult.get(inspect.stack()[2].function):
                self.rollback.executionResult[inspect.stack()[2].function] = {}
        try:
            result = func(self, *args, **kwargs)
            if inspect.stack()[2].function != 'run' and inspect.stack()[2].function != '<module>':
                self.rollback.executionResult[inspect.stack()[2].function][func.__name__] = True
            else:
                self.rollback.executionResult[func.__name__] = True
            # Saving metadata in source Org VDC
            if not self.rollback.retryRollback:
                self.saveMetadataInOrgVdc()
            return result
        except Exception as err:
            raise err
    return inner


def remediate_threaded(func):
    """
        Description :   decorator to save task status. If task is using multi-threading, save metadata as follows.
                        True if all threads completed successfully
                        False if some or none threads are completed successfully
    """
    @wraps(func)
    def inner(self, *args, **kwargs):
        # If True, return; If False/None, continue
        if self.rollback.metadata.get(func.__name__):
            return

        if self.rollback.metadata and not hasattr(self.rollback, 'retry') and not self.rollback.retryRollback:
            logger.info('Continuing migration of NSX-V backed Org VDC to NSX-T backed from {}.'.format(self.__desc__))
            self.rollback.retry = True

        # Getting vcd rest api session
        getSession(self)

        # Saving current number of threads
        currentThreadCount = self.thread.numOfThread
        try:
            # Setting new thread count
            self.thread.numOfThread = kwargs.get('threadCount', currentThreadCount)
            if not self.rollback.retryRollback and self.rollback.metadata.get(func.__name__) is not False:
                self.rollback.executionResult[func.__name__] = False
                self.saveMetadataInOrgVdc()

            result = func(self, *args, **kwargs)

            if not self.rollback.retryRollback and result is not METADATA_SAVE_FALSE:
                self.rollback.executionResult[func.__name__] = True
                # Saving metadata in source Org VDC
                self.saveMetadataInOrgVdc()

            return result

        except Exception as err:
            raise err

        finally:
            # Restoring thread count
            self.thread.numOfThread = currentThreadCount

    return inner


def description(desc, threadName=None):
    """
        Description : decorator to add description for a task before calling remediation decorator
    """
    def nested(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            setattr(self, '__desc__', desc)
            setattr(self, '__threadname__', threadName)
            return function(self, *args, **kwargs)
        return wrapped
    return nested


class DfwRulesAbsentError(Exception):
    pass


class VCDMigrationValidation:
    """
    Description : Class performing VMware Cloud Director NSX-V To NSX-T Migration validation
    """
    VCD_SESSION_CREATED = False

    def __init__(self, ipAddress, username, password, verify, rollback, threadObj=None, lockObj=None ,vdcName=None):
        """
        Description :   Initializer method of VMware Cloud Director Operations
        Parameters  :   ipAddress      -   ipAddress of the VMware vCloud Director (STRING)
                        username       -   Username of the VMware vCloud Director (STRING)
                        password       -   Password of the VMware vCloud Director (STRING)
                        verify         -   whether to validate certficate (BOOLEAN)
                        rollback       -   Object of rollback class which also acts as shared memory between classes (OBJECT)
                        maxThreadCount -   Number of maximum threads to be spawned (INTEGER)
                        vdcName        -   Name of the vdc which this object is associated to (STRING)
                        lockObj     -   Shared object of threading.Rlock() to implement locking for threads (OBJECT)
        """
        self.ipAddress = ipAddress
        self.username = '{}@system'.format(username)
        self.password = password
        self.verify = verify
        self.vdcName = vdcName
        self.vCDSessionId = None
        self.vcdUtils = Utilities()
        self.thread = threadObj
        self.rollback = rollback
        self.version = self._getAPIVersion()
        self.nsxVersion = None
        self.networkProviderScope = None
        self.namedDisks = dict()
        self.l3DfwRules = None
        self.dfwSecurityTags = dict()
        vcdConstants.VCD_API_HEADER = vcdConstants.VCD_API_HEADER.format(self.version)
        vcdConstants.GENERAL_JSON_CONTENT_TYPE = vcdConstants.GENERAL_JSON_CONTENT_TYPE.format(self.version)
        vcdConstants.OPEN_API_CONTENT_TYPE = vcdConstants.OPEN_API_CONTENT_TYPE.format(self.version)
        self.lock = lockObj

    def _getAPIVersion(self):
        """
        Description :   Method to get supported api version of VMware Cloud Director
        """
        try:
            url = vcdConstants.GET_API_VERSION.format(self.ipAddress)
            # get rest client object
            restClientObj = RestAPIClient(verify=self.verify)
            # get call to fetch api version
            getResponse = restClientObj.get(url, headers={'Accept': 'application/*+json'})
            # get json response
            responseDict = getResponse.json()
            if getResponse.status_code == requests.codes.ok:
                return responseDict['versionInfo'][-1]['version']
            else:
                raise Exception('Failed to fetch API version due to error {}'.format(responseDict['message']))
        except:
            raise

    def vcdLogin(self):
        """
        Description :   Method which makes the user to login into a VMware Cloud Director for performing further VCD Operations
        Returns     :   Bearer Token    - Bearer token for authorization (TUPLE)
                        Status Code     - Status code for rest api (TUPLE)
        """
        try:
            # getting the RestAPIClient object to call the REST apis
            self.restClientObj = RestAPIClient(self.username, self.password, self.verify)
            # url to create session
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.OPEN_LOGIN_URL)
            # post api call to create sessioned login with basic authentication
            loginResponse = self.restClientObj.post(url, headers={'Accept': vcdConstants.VCD_API_HEADER}, auth=self.restClientObj.auth)
            if loginResponse.status_code == requests.codes.OK:
                logger.debug('Logged in to VMware Cloud Director {}'.format(self.ipAddress))
                # saving the returned bearer token
                self.bearerToken = 'Bearer {}'.format(loginResponse.headers['X-VMWARE-VCLOUD-ACCESS-TOKEN'])
                # creating the default headers required to fire rest api
                self.headers = {'Authorization': self.bearerToken, 'Accept': vcdConstants.VCD_API_HEADER}
                self.VCD_SESSION_CREATED = True
                self.vCDSessionId = loginResponse.json().get('id', None)
                return self.bearerToken, loginResponse.status_code
            raise Exception("Failed to login to VMware Cloud Director {} with the given credentials".format(self.ipAddress))
        except requests.exceptions.SSLError as e:
            raise e
        except requests.exceptions.ConnectionError as e:
            raise e
        except Exception:
            raise

    @description("Migrating metadata from source Org VDC to target Org VDC")
    @remediate
    def migrateMetadata(self):
        """
            Description :   Migrate metadata from source org vdc to target org vdc
        """
        logger.info("Migrating metadata from source Org VDC to target Org VDC")
        sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id'].split(':')[-1]
        targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']

        # fetching raw metadata from source org vdc
        raw_metadata = self.getOrgVDCMetadata(sourceOrgVDCId, rawData=True)
        # segregating user created metadata
        metadataToMigrate = {data['Key']: [data['TypedValue']['Value'], data['TypedValue']['@xsi:type'], data.get('Domain')] for data in raw_metadata
                             if not re.search(r'-v2t$', data['Key'])}
        if metadataToMigrate:
            # Creating metadata in target org vdc
            self.createMetaDataInOrgVDC(targetOrgVDCId, metadataDict=metadataToMigrate, migration=True)
            logger.debug("Successfully migrated metadata from source Org VDC to target Org VDC")
        else:
            logger.debug("No user metadata present in source Org VDC to migrate to target Org VDC")
        logger.info('Successfully prepared Target VDC.')

    @isSessionExpired
    def getOrgVDCMetadata(self, orgVDCId, wholeData=False, entity='Org VDC', domain='all', rawData=False):
        """
        Description :   Gets Metadata in the specified Organization VDC
        Parameters  :   orgVDCId    -   Id of the Organization VDC (STRING)
                        wholeData   -   key that decides which metadata is required i.e. whole data or only created by migration tool (BOOLEAN)
                        domain      -   key used to fetch domain specific metadata all/system/general (STRING)
                        rawData     -   key used to fetch raw metadata Organization VDC (STRING)
        Returns     :   metadata    -   key value pair of metadata in Organization VDC (DICT)
        """
        try:
            metaData = {}
            # spliting org vdc id as per the requirement of xml api
            orgVDCId = orgVDCId.split(':')[-1]
            # url to fetch metadata from org vdc
            if 'disk' in entity:
                url = "{}{}".format(
                    vcdConstants.XML_API_URL.format(self.ipAddress),
                    vcdConstants.META_DATA_IN_DISK_BY_ID.format(orgVDCId))
            else:
                url = "{}{}".format(
                    vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                    vcdConstants.META_DATA_IN_ORG_VDC_BY_ID.format(orgVDCId))

            # get api to fetch meta data from org vdc
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                if responseDict['Metadata'].get('MetadataEntry'):
                    metaDataList = responseDict['Metadata']['MetadataEntry'] if isinstance(responseDict['Metadata']['MetadataEntry'], list) else [responseDict['Metadata']['MetadataEntry']]
                    if rawData:
                        return metaDataList
                    for data in metaDataList:
                        if domain == 'general' and data.get('Domain'):
                            continue
                        if domain == 'system' and not data.get('Domain'):
                            continue
                        metadataKey = data['Key']
                        metadataValue = data['TypedValue']['Value']
                        if not wholeData:
                            if not re.search(r'-v2t$', metadataKey):
                                continue
                            # Replacing -system-v2t postfix with empty string
                            if re.search(r'-system-v2t$', metadataKey):
                                metadataKey = metadataKey.replace('-system-v2t', '')
                            else:
                                # Replacing -v2t postfix with empty string
                                metadataKey = metadataKey.replace('-v2t', '')
                            # Checking and restoring api data from metadata
                            if '&amp;' in data['TypedValue']['Value']:
                                metadataValue = metadataValue.replace('&amp;', '&')

                            # Converting python objects back from string
                            try:
                                metadataValue = eval(metadataValue)
                            except (SyntaxError, NameError, ValueError):
                                pass

                        metaData[metadataKey] = metadataValue
                return metaData
            raise Exception("Failed to retrieve metadata")
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCGroup(self):
        """
        Description: Fetch all DC groups present in vCD
        """
        try:
            # url to get Org vDC groups
            url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VDC_GROUPS)
            self.headers['Content-Type'] = 'application/json'
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
                pageNo = 1
                pageSizeCount = 0
                resultList = []
            else:
                errorDict = response.json()
                raise Exception("Failed to get target org VDC Group '{}' ".format(errorDict['message']))

            logger.debug('Getting data center group details')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.VDC_GROUPS, pageNo,
                                                        25)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('DataCenter group details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    errorDict = response.json()
                    raise Exception("Failed to get target org VDC Group '{}' ".format(errorDict['message']))
            logger.debug('Total data center group details result count = {}'.format(len(resultList)))
            logger.debug('DataCenter group details successfully retrieved')
            return resultList
        except:
            raise

    @isSessionExpired
    def deleteMetadataApiCall(self, key, orgVDCId, entity='Org VDC'):
        """
            Description :   API call to delete Metadata from the specified Organization VDC
            Parameters  :   key         -   Metadata key to be deleted (STRING)
                            orgVDCId    -   Id of the Organization VDC (STRING)
        """
        try:
            orgVDCId = orgVDCId.split(":")[-1]

            if re.search(r'-v2t$', key):
                if 'disk' in entity:
                    base_url = "{}{}".format(
                        vcdConstants.XML_API_URL.format(self.ipAddress),
                        vcdConstants.META_DATA_IN_DISK_BY_ID.format(orgVDCId))
                else:
                    base_url = "{}{}".format(
                        vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                        vcdConstants.META_DATA_IN_ORG_VDC_BY_ID.format(orgVDCId))

                if re.search(r'-system-v2t$', key):
                    # url for system domain metadata delete api call
                    url = base_url + "/SYSTEM/{}".format(key)
                else:
                    # url to delete metadata from org vdc
                    url = base_url + "/{}".format(key)
                response = self.restClientObj.delete(url, self.headers)
                if response.status_code == requests.codes.accepted:
                    responseDict = xmltodict.parse(response.content)
                    task = responseDict["Task"]
                    taskUrl = task["@href"]
                    if taskUrl:
                        # checking the status of the creating meta data in org vdc task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Deleted metadata with key: {} successfully'.format(key))
                else:
                    raise Exception('Failed to delete metadata key: {}'.format(key))
        except Exception:
            raise

    @isSessionExpired
    def deleteMetadata(self, orgVDCId, entity='Org VDC'):
        """
            Description :   Delete Metadata from the specified Organization VDC
            Parameters  :   orgVDCId    -   Id of the Organization VDC (STRING)
        """
        try:
            # spliting org vdc id as per the requirement of xml api
            orgVDCId = orgVDCId.split(':')[-1]
            metadata = self.getOrgVDCMetadata(orgVDCId, entity=entity, wholeData=True)
            if metadata:
                logger.info(f"Rollback: Deleting metadata from source {entity}")
                for key in metadata.keys():
                    # spawn thread for deleting metadata key api call
                    self.thread.spawnThread(self.deleteMetadataApiCall, key, orgVDCId, entity)
                # halting main thread till all the threads complete execution
                self.thread.joinThreads()
                # checking if any of the threads raised any exception
                if self.thread.stop():
                    raise Exception(f"Failed to delete metadata from source {entity}")
            else:
                logger.debug(f"No metadata present to delete in source {entity}")

        except Exception:
            raise

    @isSessionExpired
    def createMetaDataInOrgVDC(self, orgVDCId, metadataDict, entity='Org VDC', domain='general', migration=False):
        """
        Description :   Creates/Updates Metadata in the specified Organization VDC
                        If the specified key doesnot already exists in Org VDC then creates new (Key, Value) pair
                        Else updates the specified existing key with the new metadatValue
        Parameters  :   orgVDCId        -   Id of the Organization VDC (STRING)
                        metadataDict    -   Metadata key value pairs (DICT)
                        domain          -   Domain in which metadata is to be created general/system (STRING)
                        migration       -   Key that defines metadata creation is part of migration or not (BOOLEAN)
        """
        try:
            if metadataDict:
                if self.headers.get('Content-Type'):
                    del self.headers['Content-Type']
                # spliting org vdc id as per the requirement of xml api
                orgVDCId = orgVDCId.split(':')[-1]
                # url to create meta data in org vdc
                if entity == 'disk':
                    url = "{}{}".format(
                        vcdConstants.XML_API_URL.format(self.ipAddress),
                        vcdConstants.META_DATA_IN_DISK_BY_ID.format(orgVDCId))
                else:
                    url = "{}{}".format(
                        vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                        vcdConstants.META_DATA_IN_ORG_VDC_BY_ID.format(orgVDCId))

                filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')

                # creating payload for domain in metadata
                domainPayload = '' if domain == 'general' else "<Domain visibility='PRIVATE'>SYSTEM</Domain>"
                xmlPayload = ''
                for key, value in metadataDict.items():
                    if not migration:
                        if domain.lower().strip() == 'system':
                            # appending -system-v2t to metadata key of system domain for identification of migration tool metadata
                            key += '-system-v2t'
                        else:
                            # appending -vdt to metadata key for identification of migration tool metadata
                            key += '-v2t'
                        # replacing & with escape value for XML based API's
                        if '&' in str(value):
                            value = eval(str(value).replace('&', '&amp;'))
                        metadataType = 'MetadataStringValue'
                    else:
                        # Fetch domain of user-defined metadata and create payload from it
                        value, metadataType, domain = value
                        if domain:
                            domainPayload = f"<Domain visibility='{domain['@visibility']}'>{domain['#text']}</Domain>"
                        else:
                            domainPayload = ''
                    payloadDict = {'key': key, 'value': value, 'domain': domainPayload, 'metadataType': metadataType}
                    # creating payload data
                    xmlPayload += self.vcdUtils.createPayload(filePath,
                                                              payloadDict,
                                                              fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.CREATE_ORG_VDC_METADATA_ENTRY_TEMPLATE).strip('"')

                payloadDict = {'MetadataEntry': xmlPayload}
                # creating payload data
                payloadData = self.vcdUtils.createPayload(filePath,
                                                          payloadDict,
                                                          fileType='yaml',
                                                          componentName=vcdConstants.COMPONENT_NAME,
                                                          templateName=vcdConstants.CREATE_ORG_VDC_METADATA_TEMPLATE)

                payloadData = json.loads(payloadData)

                # post api to create meta data in org vdc
                response = self.restClientObj.post(url, self.headers, data=payloadData)
                responseDict = xmltodict.parse(response.content)
                if response.status_code == requests.codes.accepted:
                    task = responseDict["Task"]
                    taskUrl = task["@href"]
                    if taskUrl:
                        # checking the status of the creating meta data in org vdc task
                        self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug("Created Metadata in {} {} successfully".format(entity, orgVDCId))
                    return response
                raise Exception("Failed to create the Metadata in {}: {}".format(
                    entity, responseDict['Error']['@message']))
            else:
                return
        except Exception:
            raise

    def metadataCleanup(self, metadata):
        """
            Description: Cleanup of metadata after its generation to reduce overall size of metadata
            Parameters: metadata that needs cleanup for size reduction - (DICT)
        """
        # Keys to be checked and removed is present cause these lead to unnecessary data
        keysToBeRemoved = ['@rel', 'Link', 'Settings', 'OrgAssociations', 'Networks',
                           'RightReferences', 'RoleReferences', 'VCloudExtension', 'Error', 'Tasks', 'Users',
                           'AvailableNetworks', 'MaxComputePolicy', 'ProviderVdcReference', 'ResourcePoolRefs',
                           '@default', '#text', 'Catalogs', 'ResourceEntities']

        if isinstance(metadata, (dict, OrderedDict)):
            # Removing capabilties if present from source and target org vdc
            if metadata.get('sourceOrgVDC') and metadata.get('sourceOrgVDC').get('Capabilities'):
                del metadata['sourceOrgVDC']['Capabilities']

            if metadata.get('targetOrgVDC') and metadata.get('targetOrgVDC').get('Capabilities'):
                del metadata['targetOrgVDC']['Capabilities']

            for key in list(metadata.keys()):
                # If key present in list of keys to be removed then delete its key value pair from metadata
                if key in keysToBeRemoved:
                    # Delete key from metadata dictionary
                    del metadata[key]
                else:
                    self.metadataCleanup(metadata[key])

    @isSessionExpired
    def saveMetadataInOrgVdc(self, force=False):
        """
            Description: Saving data necessary for continuation of migration and for rollback in metadata of source Org VDC
        """

        try:
            if force or self.rollback.executionResult:
                # getting the source org vdc urn
                sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']

                metadata = self.rollback.metadata

                if self.rollback.apiData:
                    # removing unnecessary data from api data to reduce metadata size
                    self.metadataCleanup(self.rollback.apiData)
                    # saving api data in metadata
                    self.createMetaDataInOrgVDC(sourceOrgVDCId, metadataDict=self.rollback.apiData)

                # saving execution result in metadata
                for key, value in self.rollback.executionResult.items():
                    if isinstance(value, dict) and metadata.get(key):
                        combinedSubtask = {**metadata.get(key), **value}
                        self.rollback.executionResult[key] = combinedSubtask

                self.createMetaDataInOrgVDC(sourceOrgVDCId,
                                                    metadataDict=self.rollback.executionResult, domain='system')

        except Exception as err:
            raise Exception('Failed to save metadata in source Org VDC due to error - {}'.format(err))

    @isSessionExpired
    def getOrgUrl(self, orgName):
        """
        Description : Retrieves the Organization URL details
        Parameters  : orgName   - Name of the Organization (STRING)
        Returns     : orgUrl    - Organization URL (STRING)
        """
        logger.debug('Getting Organization {} Url'.format(orgName))
        # admin xml url
        url = vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress)
        try:
            # get api call to retrieve organization details
            response = self.restClientObj.get(url, headers=self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                # retrieving organization references
                responseDict = responseDict['VCloud']['OrganizationReferences']['OrganizationReference']
                if isinstance(responseDict, dict):
                    responseDict = [responseDict]
                for record in responseDict:
                    # retrieving the orgnization details of organization specified in orgName
                    if record['@name'] == orgName:
                        orgUrl = record['@href']
                        logger.debug('Organization {} url {} retrieved successfully'.format(orgName, orgUrl))
                        # returning the organization url
                        return orgUrl
            raise Exception("Failed to retrieve Organization {} url".format(orgName))
        except Exception:
            raise

    def getOrgId(self, orgName):
        """
        Description : Retrieves the Organization ID by name
        Parameters  : orgName   - Name of the Organization (STRING)
        Returns     : orgID    - Organization ID (STRING)
        """
        logger.debug('Getting Organization {} ID'.format(orgName))
        orgUrl = self.getOrgUrl(orgName)
        # get api call to retrieve the organization details
        orgResponse = self.restClientObj.get(orgUrl, headers=self.headers)
        orgResponseDict = xmltodict.parse(orgResponse.content)

        # retrieving the organization ID
        orgId = orgResponseDict['AdminOrg']['@id']
        logger.debug('Organization {} ID {} retrieved successfully'.format(orgName, orgId))

        return orgId

    def getOrgVDCUrl(self, orgUrl, orgVDCName, saveResponse=True):
        """
        Description : Get Organization VDC Url
        Parameters  : orgUrl        - Organization URL (STRING)
                      orgVDCName    - Name of the Organization VDC (STRING)
        Returns     : orgVDCUrl     - Organization VDC URL (STRING)
        """
        try:
            orgVDCUrl = ''
            data = {}
            logger.debug('Getting Organization VDC Url {}'.format(orgVDCName))
            # get api call to retrieve org vdc details of specified orgVdcName
            response = self.restClientObj.get(orgUrl, headers=self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                data = self.rollback.apiData
                if not data and saveResponse:
                    # creating 'Organization' key to save organization info
                    data['Organization'] = responseDict['AdminOrg']
                if not responseDict['AdminOrg']['Vdcs']:
                    raise Exception('No Org VDC exist in the organization')
                responseDict = responseDict['AdminOrg']['Vdcs']['Vdc']
                if isinstance(responseDict, dict):
                    responseDict = [responseDict]
                for response in responseDict:
                    # checking for orgVDCName in the responseDict, if found then returning the orgVDCUrl
                    if response['@name'] == orgVDCName:
                        orgVDCUrl = response['@href']
                        logger.debug('Organization VDC {} url {} retrieved successfully'.format(orgVDCName, orgVDCUrl))
                if not orgVDCUrl:
                    raise Exception('Org VDC {} does not belong to this organization {}'.format(orgVDCName, orgUrl))
                return orgVDCUrl
            raise Exception("Failed to retrieve Organization VDC {} url".format(orgVDCName))
        except Exception:
            raise

    def getOrgVDCDetails(self, orgUrl, orgVDCName, orgVDCType, saveResponse=True):
        """
        Description :   Gets the details of the Organizational VDC
        Parameters  : orgUrl        - Organization URL (STRING)
                      orgVDCName    - Name of the Organization VDC (STRING)
                      orgVDCType    - type of org vdc whether sourceOrgVDC or targetOrgVDC
        """
        try:
            logger.debug('Getting Organization VDC {} details'.format(orgVDCName))
            # retrieving the org vdc url
            self.orgVDCUrl = self.getOrgVDCUrl(orgUrl, orgVDCName, saveResponse)
            # get api call to retrieve the orgVDCName details
            response = self.restClientObj.get(self.orgVDCUrl, headers=self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                if saveResponse:
                    # loading the existing data from api data dict
                    data = self.rollback.apiData
                    data[orgVDCType] = responseDict['AdminVdc']
                    logger.debug('Retrieved Organization VDC {} details successfully'.format(orgVDCName))
                # returning the orgVDCName details
                return responseDict['AdminVdc']['@id']
            raise Exception("Failed to retrieve details of Organization VDC {} {}".format(orgVDCName,
                                                                                          responseDict['Error']['@message']))
        except Exception:
            raise

    @isSessionExpired
    def validateOrgVDCFastProvisioned(self):
        """
        Description :   Validates whether fast provisioning is enabled on the Org VDC
        """
        try:
            data = self.rollback.apiData
            # checking if the source org vdc uses fast provisioning, if so raising exception
            if data['sourceOrgVDC']['UsesFastProvisioning'] == "true":
                raise Exception("Fast Provisioning enabled on source Org VDC. Will not migrate fast provisioned org vdc")
            logger.debug("Validated Succesfully, Fast Provisioning is not enabled on source Org VDC")
        except Exception:
            raise

    @isSessionExpired
    def getSourceExternalNetwork(self, sourceOrgVDCId):
        """
        Description :   Gets the details of external networks
        Parameters  :   networkName - Name of the external network (STRING)
                        isDummyNetwork - is the network dummy (BOOL)
        """
        try:
            sourceEdgeGatewayIdList = self.getOrgVDCEdgeGatewayId(sourceOrgVDCId)
            sourceExternalNetworkNames = self.getSourceExternalNetworkName(sourceEdgeGatewayIdList)
            sourceExternalNetworkData = []

            # iterating over all the external networks
            for response in self.fetchAllExternalNetworks():
                # checking if networkName is present in the list, if present saving the specified network's details to apiOutput.json
                if response['name'] in sourceExternalNetworkNames:
                    sourceExternalNetworkData.append(response)
                    logger.debug("Retrieved External Network {} details Successfully".format(response['name']))
            self.rollback.apiData['sourceExternalNetwork'] = sourceExternalNetworkData
            return sourceExternalNetworkData
        except Exception:
            raise

    @isSessionExpired
    def getExternalNetwork(self, networkName, isDummyNetwork=False, validateVRF=False):
        """
        Description :   Gets the details of external networks
        Parameters  :   networkName - Name of the external network (STRING)
                        isDummyNetwork - is the network dummy (BOOL)
                        validateVRF - Flag that decides to validate vrf backed external network (BOOL)
        """
        try:
            key = None
            logger.debug("Getting External Network {} details ".format(networkName))
            # iterating over all the external networks
            for response in self.fetchAllExternalNetworks():
                # checking if networkName is present in the list
                if response['name'] == networkName:
                    if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                        key = 'targetExternalNetwork' if response['networkBackings']['values'][0]['backingTypeValue'] in ['NSXT_TIER0', 'NSXT_VRF_TIER0'] else 'sourceExternalNetwork'
                        if response['networkBackings']['values'][0]['backingTypeValue'] == 'NSXT_VRF_TIER0' and validateVRF:
                            logger.warning('Target External Network {} is VRF backed.'.format(networkName))
                    elif float(self.version) <= float(vcdConstants.API_VERSION_PRE_ZEUS):
                        key = 'targetExternalNetwork' if response['networkBackings']['values'][0]['backingType'] in ['NSXT_TIER0', 'NSXT_VRF_TIER0'] else 'sourceExternalNetwork'
                    data = self.rollback.apiData
                    if isDummyNetwork:
                        key = 'dummyExternalNetwork'
                    data[key] = response
                    logger.debug("Retrieved External Network {} details Successfully".format(networkName))
                    return response
            if key is None:
                return Exception('External Network: {} not present'.format(networkName))
        except Exception:
            raise

    @isSessionExpired
    def getNsxtManagerId(self, pvdcName):
        """
            Description :   Gets the id of NSXT manager of provider vdc
            Parameters  :   pvdcName - Name of the provider vdc (STRING)
        """
        try:
            logger.debug("Getting NSXT manager id of Provider VDC {}".format(pvdcName))
            # url to get details of the all provider vdcs
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.PROVIDER_VDC)
            # get api call to retrieve the all provider vdc details
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
                pageNo = 1
                pageSizeCount = 0
                resultList = []
            else:
                errorDict = response.json()
                raise Exception("Failed to get Provider VDC {} details {}".format(pvdcName,
                                                                         errorDict['message']))
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.PROVIDER_VDC, pageNo, 25)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('Provider VDC details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    errorDict = response.json()
                    raise Exception('Failed to get Provider VDC {} details {}'.format(pvdcName, errorDict['message']))

            # iterating over all provider vdcs to find if the specified provider vdc details exists
            for response in resultList:
                if response['name'] == pvdcName:
                    logger.debug("Retrieved Provider VDC {} details successfully".format(pvdcName))
                    # returning nsx-t manager id
                    return response['nsxTManager']['id']
            else:
                raise Exception("No provider VDC '{}' found".format(pvdcName))
        except Exception:
            raise

    @isSessionExpired
    def getProviderVDCId(self, pvdcName=str(), returnRaw=False):
        """
        Description :   Gets the id of provider vdc
        Parameters  :   pvdcName - Name of the provider vdc (STRING)
                        returnRaw - Bool that decides to return whole data or not
        """
        try:
            logger.debug("Getting Provider VDC {} id".format(pvdcName))
            # url to get details of the all provider vdcs
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.PROVIDER_VDC)
            # get api call to retrieve the all provider vdc details
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
                pageNo = 1
                pageSizeCount = 0
                resultList = []
            else:
                errorDict = response.json()
                raise Exception("Failed to get Provider VDC {} details {}".format(pvdcName,
                                                                         errorDict['message']))
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.PROVIDER_VDC, pageNo, 25)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('edge cluster details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    errorDict = response.json()
                    raise Exception("Failed to get Provider VDC details : {}".format(errorDict['message']))

            # iterating over all provider vdcs to find if the specified provider vdc details exists
            for response in responseDict['values']:
                if returnRaw:
                    return responseDict['values']
                if response['name'] == pvdcName:
                    logger.debug("Retrieved Provider VDC {} id successfully".format(pvdcName))
                    # returning provider vdc id of specified pvdcName & nsx-t manager
                    return response['id'], bool(response['nsxTManager'])
            else:
                raise Exception("No provider VDC '{}' found".format(pvdcName))
        except Exception:
            raise

    def getProviderVDCDetails(self, pvdcId, isNSXTbacked=False):
        """
        Description :   Gets the id of provider vdc
        Parameters  :   pvdcId - Id of the provider vdc (STRING)
                        isNSXTbacked - True if NSX-T manager backed else False (BOOL)
        """
        try:
            logger.debug("Getting Provider VDC {} details".format(pvdcId))
            # splitting the provider vdc id as per the requirements of xml api
            providervdcId = pvdcId.split(':')[-1]
            # url to retrieve the specified provider vdc details
            url = "{}{}/{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                   vcdConstants.PROVIDER_VDC_XML,
                                   providervdcId)
            # get api call retrieve the specified provider vdc details
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                key = 'targetProviderVDC' if isNSXTbacked else 'sourceProviderVDC'
                # loading existing data from apiOutput.json
                self.thread.acquireLock()
                data = self.rollback.apiData
                # Save only capabilities of source provider vdc
                if key == 'sourceProviderVDC':
                    data[key] = responseDict['ProviderVdc']['Capabilities']
                data[key] = responseDict['ProviderVdc']
                self.thread.releaseLock()
                logger.debug("Provider VDC {} details retrieved successfully".format(responseDict['ProviderVdc']['@name']))
                if not isNSXTbacked:
                    # warning the user that source pvdc is disabled which may break rollback
                    if responseDict['ProviderVdc']['IsEnabled'] == "false":
                        logger.warning("Source PVDC '{}' is disabled".format(responseDict['ProviderVdc']['@name']))
                return responseDict['ProviderVdc']
            raise Exception('Failed to get Provider VDC details')
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCvAppsList(self, orgVDCId):
        """
        Description :   Retrieves the list of vApps in the Source Org VDC
        Returns     :   Returns Source vapps list (LIST)
        """
        try:
            logger.debug("Getting Org VDC vApps List")

            orgVDCId = orgVDCId.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(orgVDCId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
            else:
                raise Exception('Error occurred while retrieving Org VDC - {} details'.format(orgVDCId))
            # getting list instance of resources in the source org
            if responseDict['AdminVdc'].get('ResourceEntities'):
                orgVDCEntityList = responseDict['AdminVdc']['ResourceEntities']['ResourceEntity'] \
                    if isinstance(responseDict['AdminVdc']['ResourceEntities']['ResourceEntity'], list) else [
                    responseDict['AdminVdc']['ResourceEntities']['ResourceEntity']]
                if orgVDCEntityList:
                    # getting list of source vapps
                    sourceVappList = [vAppEntity for vAppEntity in orgVDCEntityList if
                                    vAppEntity['@type'] == vcdConstants.TYPE_VAPP]
                    return sourceVappList
            else:
                return []
        except Exception:
            raise

    @isSessionExpired
    def getNsxDetails(self, nsxIpAddress):
        """
        Description : Get NSX-T manager details from VMware Cloud Director
        Parameters :  nsxIpAddress - IP Address of NSX-T Manager (IP ADDRESS)
        """
        try:
            currentNsxManager = dict()
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.NSX_MANAGERS)
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            allNsxtManager = responseDict['vmext:NsxTManagers']['vmext:NsxTManager'] if isinstance(responseDict['vmext:NsxTManagers']['vmext:NsxTManager'], list) else [responseDict['vmext:NsxTManagers']['vmext:NsxTManager']]
            for eachNsxManager in allNsxtManager:
                if nsxIpAddress in eachNsxManager['vmext:Url']:
                    # Network provider scope to be used for data center group creation
                    self.networkProviderScope = eachNsxManager['vmext:NetworkProviderScope']
                    self.nsxVersion = eachNsxManager['vmext:Version']
                    currentNsxManager = eachNsxManager
                    break
            if currentNsxManager == {}:
                raise Exception('Incorrect NSX-T IP Address in input file. Please check if the NSX-T IP Address matches the one in NSXT-Managers in vCD')
        except Exception:
            raise

    @isSessionExpired
    def validateExternalNetworkIsVRFLiteBacked(self):
        try:
            data = self.rollback.apiData
            if data['targetExternalNetwork']['networkBackings']['values'][0]['backingTypeValue'] == "NSXT_VRF_TIER0":
                logger.info("Target external network: {} is VRF-Lite backed".format(data['targetExternalNetwork']['name']))
                return True
        except Exception:
            raise


    @isSessionExpired
    def validateVappFencingMode(self, sourceOrgVDCId):
        """
            Description :   Validate if fencing is enable on vApps in source OrgVDC
            Notes       :   It does this by checking status(true/false) of firewallService in the org vdc network of vapp
                            Because fence mode can be enabled on vapp if and only if any org vdc network is present in the vapp
                            Also FirewallService will by default be enabled on the fenced org vdc networks
                            So checking the vappData['NetworkConfigSection']['NetworkConfig']['Configuration']['Features']['FirewallService']['IsEnabled'] is true
        """
        try:
            vAppFencingList = list()
            allVappList = self.getOrgVDCvAppsList(sourceOrgVDCId)

            # iterating over the vapps in the source org vdc
            for eachVapp in allVappList:
                # get api call to get the vapp details
                response = self.restClientObj.get(eachVapp['@href'], self.headers)
                responseDict = xmltodict.parse(response.content)
                vAppData = responseDict['VApp']

                logger.debug('Checking fencing on vApp: {}'.format(eachVapp['@name']))
                # checking for the networks present in the vapp
                if vAppData.get('NetworkConfigSection'):
                    if vAppData['NetworkConfigSection'].get('NetworkConfig'):
                        networksInvApp = vAppData['NetworkConfigSection']['NetworkConfig'] if isinstance(vAppData['NetworkConfigSection']['NetworkConfig'], list) else [vAppData['NetworkConfigSection']['NetworkConfig']]
                        # iterating over the networks present in vapp(example:- vapp networks, org vdc networks, etc)
                        for network in networksInvApp:
                            # checking if the network is org vdc network(i.e if network's name and its parent network name is same means the network is org vdc network)
                            # here our interest networks are only org vdc networks present in vapp
                            if network['Configuration'].get('ParentNetwork') and network['@networkName'] == network['Configuration']['ParentNetwork']['@name']:
                                if network['Configuration'].get('Features') and network['Configuration']['Features'].get('FirewallService'):
                                    # since FirewallService is enabled on org vdc networks if fence mode is enabled, checking if ['FirewallService']['IsEnabled'] attribute is true
                                    if network['Configuration']['Features']['FirewallService']['IsEnabled'] == 'true':
                                        # adding the vapp name in the vAppFencingList to raise the exception
                                        vAppFencingList.append(eachVapp['@name'])
                                        # this will logged number of times equal to org vdc networks present in vapp before enabling the fence mode
                                        logger.debug("Fence mode is enabled on vApp: '{}'".format(eachVapp['@name']))

            if vAppFencingList:
                raise Exception('Fencing mode is enabled on vApp: {}'.format(', '.join(set(vAppFencingList))))
            else:
                logger.debug('vApp fencing is disabled on all vApps')
        except Exception:
            raise

    @isSessionExpired
    def validateOrgVDCNSXbacking(self, orgVDCId, providerVDCId, isNSXTbacked):
        """
        Description : Validate whether Org VDC is NSX-V or NSX-T backed
        Parameters : orgVDCId         - Org VDC id (STRING)
                     providerVDCId    - ProviderVDC id (STRING)
                     isNSXTbacked     - True if provider VDC is NSX-T backed else False (BOOL)
        """
        try:
            # splitting the source org vdc id as per the requirements of xml api
            orgVdcId = orgVDCId.split(':')[-1]
            # url to retrieve the specified provider vdc details
            url = '{}{}'.format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(orgVdcId))
            # get api call retrieve the specified provider vdc details
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                responseProviderVDCId = responseDict['AdminVdc']['ProviderVdcReference']['@id']
                # if NSXTbacked is false
                if not isNSXTbacked:
                    # checking if source provider vdc is nsx-v backed, if not then raising exception
                    if responseProviderVDCId == providerVDCId:
                        logger.debug("Validated successfully source Org VDC {} is NSX-V backed.".format(responseDict['AdminVdc']['@name']))
                        return
                    else:
                        raise Exception("Source Org VDC {} is not NSX-V backed.".format(responseDict['AdminVdc']['@name']))
                else:
                    # checking if target provider vdc is nsx-t backed, if not then raising exception
                    if responseProviderVDCId == providerVDCId:
                        logger.debug("Validated successfully target Org VDC {} is NSX-T backed.".format(responseDict['AdminVdc']['@name']))
                        return
                    else:
                        raise Exception("Target Org VDC {} is not NSX-T backed.".format(responseDict['AdminVdc']['@name']))
            else:
                raise Exception('Failed to validate Org VDC NSX backing type.')
        except Exception:
            raise

    @isSessionExpired
    def validateTargetProviderVdc(self):
        """
        Description :   Validates whether the target Provider VDC is Enabled
        """
        try:
            # reading api data from metadata
            data = self.rollback.apiData
            # checking if target provider vdc is enabled, if not raising exception
            if data['targetProviderVDC']['IsEnabled'] != "true":
                raise Exception("Target Provider VDC is not enabled")
            logger.debug("Validated successfully target Provider VDC is enabled")
        except Exception:
            raise

    @isSessionExpired
    def disableOrgVDC(self, orgVDCId):
        """
        Description :   Disable the Organization vdc
        Parameters  :   orgVDCId - Id of the source organization vdc (STRING)
        """
        try:
            # reading api from metadata
            data = self.rollback.apiData
            isEnabled = data['sourceOrgVDC']['IsEnabled']
            orgVDCName = data['sourceOrgVDC']['@name']
            # checking if the org vdc is already disabled, if not then disabling it
            if isEnabled == "false":
                logger.warning('Source Org VDC - {} is already disabled'.format(orgVDCName))
            else:
                vdcId = orgVDCId.split(':')[-1]
                # url to disable the org vdc
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.ORG_VDC_DISABLE.format(vdcId))
                # post api call to disable org vdc
                response = self.restClientObj.post(url, self.headers)
                if response.status_code == requests.codes['no_content']:
                    logger.debug("Source Org VDC {} disabled successfully".format(orgVDCName))
                else:
                    errorDict = xmltodict.parse(response.content)
                    raise Exception('Failed to disable Source Org VDC - {}'.format(errorDict['Error']['@message']))
        except Exception:
            raise
        else:
            return True

    @description("Disabling target Org VDC if source Org VDC was in disabled state")
    @remediate
    def disableTargetOrgVDC(self, rollback=False):
        """
        Description :   Disable the Organization vdc
        Parameters  :   orgVDCId - Id of the target organization vdc (STRING)
        """
        try:
            if rollback and not self.rollback.metadata.get("disableTargetOrgVDC"):
                return
            # reading api from metadata
            data = self.rollback.apiData
            isEnabled = data['sourceOrgVDC']['IsEnabled']
            # Fetching target VDC Id
            orgVDCId = data['targetOrgVDC']['@id']

            if rollback and isEnabled == "false":
                vdcId = orgVDCId.split(':')[-1]
                # enabling target org vdc if disabled to handle rollback
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.ENABLE_ORG_VDC.format(vdcId))
                # post api call to enable source org vdc
                response = self.restClientObj.post(url, self.headers)
                if response.status_code == requests.codes.no_content:
                    logger.debug("Target Org VDC Enabled successfully")
                else:
                    responseDict = xmltodict.parse(response.content)
                    raise Exception("Failed to Enable Target Org VDC: {}".format(responseDict['Error']['@message']))
            elif isEnabled == "false":
                # disabling the target org vdc if and only if the source org vdc was initially in disabled state, else keeping target org vdc enabled
                targetOrgVDCName = data['targetOrgVDC']['@name']
                logger.debug("Disabling the target org vdc since source org vdc was in disabled state")
                vdcId = orgVDCId.split(':')[-1]
                # url to disable the org vdc
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.ORG_VDC_DISABLE.format(vdcId))
                # post api call to disable org vdc
                response = self.restClientObj.post(url, self.headers)
                if response.status_code == requests.codes['no_content']:
                    logger.debug("Target Org VDC {} disabled successfully".format(targetOrgVDCName))
                else:
                    errorDict = xmltodict.parse(response.content)
                    raise Exception('Failed to disable Target Org VDC - {}'.format(errorDict['Error']['@message']))
        except Exception:
            logger.error(traceback.format_exc())
            raise

    @isSessionExpired
    def validateVMPlacementPolicy(self, sourceOrgVDCId):
        """
        Description : Validate whether source Org VDC placement policy exist in target PVDC
        Parameters  : sourceOrgVDCId   - Id of the source org vdc (STRING)
        """
        try:
            targetPVDCComputePolicyList = []
            # reading api data from metadata
            data = self.rollback.apiData
            orgVdcId = sourceOrgVDCId.split(':')[-1]
            # url to retrieve compute policies of source org vdc
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_COMPUTE_POLICY.format(orgVdcId))
            # get api call to retrieve source org vdc compute policies
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                data['sourceOrgVDCComputePolicyList'] = responseDict['VdcComputePolicyReferences']['VdcComputePolicyReference']
            sourceOrgVDCName = data['sourceOrgVDC']['@name']
            targetProviderVDCName = data['targetProviderVDC']['@name']
            targetProviderVDCId = data['targetProviderVDC']['@id']
            sourcePolicyList = data['sourceOrgVDCComputePolicyList']
            sourceComputePolicyList = [sourcePolicyList] if isinstance(sourcePolicyList, dict) else sourcePolicyList
            allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
            orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(allOrgVDCComputePolicesList, dict) else allOrgVDCComputePolicesList
            targetTemporaryList = []
            # iterating over the org vdc compute policies
            for eachComputePolicy in orgVDCComputePolicesList:
                # checking if the org vdc compute policy's provider vdc is same as target provider vdc
                if eachComputePolicy["pvdcId"] == targetProviderVDCId:
                    # iterating over the source org vdc compute policies
                    for computePolicy in sourceComputePolicyList:
                        if computePolicy['@name'] == eachComputePolicy['name']:
                            # handling the multiple occurrences of same policy, but adding the policy just once in the  list 'targetPVDCComputePolicyList'
                            if eachComputePolicy['name'] not in targetTemporaryList:
                                targetTemporaryList.append(eachComputePolicy['name'])
                                targetPVDCComputePolicyList.append(eachComputePolicy)

            # creating list of source org vdc compute policies excluding system default
            sourceOrgVDCComputePolicyList = [sourceComputePolicy for sourceComputePolicy in sourceComputePolicyList if sourceComputePolicy['@name'] != 'System Default']
            sourceOrgVDCPlacementPolicyList = []
            sourceTemporaryList = []
            # iterating over source org vdc compute policies
            for vdcComputePolicy in sourceOrgVDCComputePolicyList:
                # get api call to retrieve compute policy details
                response = self.restClientObj.get(vdcComputePolicy['@href'], self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    if not responseDict['isSizingOnly']:
                        # handling the multiple occurrences of same policy, but adding the policy just once in the  list 'sourceOrgVDCPlacementPolicyList'
                        if vdcComputePolicy['@name'] not in sourceTemporaryList:
                            sourceTemporaryList.append(vdcComputePolicy['@name'])
                            sourceOrgVDCPlacementPolicyList.append(vdcComputePolicy)
            # deleting both the temporary list, since no longer needed
            del targetTemporaryList
            del sourceTemporaryList
            if len(sourceOrgVDCPlacementPolicyList) != len(targetPVDCComputePolicyList):
                raise Exception('Target PVDC - {} does not have source Org VDC - {} placement policies in it.'.format(targetProviderVDCName,
                                                                                                                     sourceOrgVDCName))
            if len(sourceOrgVDCPlacementPolicyList)>0:
                logger.debug("Validated successfully, source Org VDC placement policy exist in target PVDC")
            else:
                logger.debug("No placement policies are present in source Org VDC")
        except Exception:
            raise

    @isSessionExpired
    def validateStorageProfiles(self):
        """
        Description :   Validate storage profiles of source org vdc with target provider vdc
                        Also validates the source  org vdc storage profiles which are present in target provider vdc are all enabled in target pvdc
        """
        try:
            data = self.rollback.apiData
            errorList = list()
            # retrieving source org vdc storage profiles
            sourceOrgVDCStorageProfile = [data['sourceOrgVDC']['VdcStorageProfiles']['VdcStorageProfile']] if isinstance(data['sourceOrgVDC']['VdcStorageProfiles']['VdcStorageProfile'], dict) else data['sourceOrgVDC']['VdcStorageProfiles']['VdcStorageProfile']
            # retrieving target provider vdc storage profiles
            targetPVDCStorageProfile = [data['targetProviderVDC']['StorageProfiles']['ProviderVdcStorageProfile']] if isinstance(data['targetProviderVDC']['StorageProfiles']['ProviderVdcStorageProfile'], dict) else data['targetProviderVDC']['StorageProfiles']['ProviderVdcStorageProfile']
            # creating list of source org vdc storage profiles found in target provider vdc
            storagePoliciesFound = [sourceDict for sourceDict in sourceOrgVDCStorageProfile for targetDict in
                                    targetPVDCStorageProfile if sourceDict['@name'] == targetDict['@name']]
            logger.debug("Storage Profiles Found in target Provider VDC are {}".format(storagePoliciesFound))
            # checking the length of profiles on source org vdc & storage profiles found on target provider vdc
            if len(sourceOrgVDCStorageProfile) != len(storagePoliciesFound):
                errorList.append("Storage profiles in Target PVDC should be same as those in Source Org VDC")

            # retrieving the storage profiles of the target provider vdc
            targetStorageProfiles = self.rollback.apiData['targetProviderVDC']['StorageProfiles']['ProviderVdcStorageProfile'] if isinstance(self.rollback.apiData['targetProviderVDC']['StorageProfiles']['ProviderVdcStorageProfile'], list) else [self.rollback.apiData['targetProviderVDC']['StorageProfiles']['ProviderVdcStorageProfile']]

            # list to hold the disabled storage profiles in target PVDC which are from source org vdc
            targetPVDCDisabledStorageProfiles = []
            # iterating over the source org vdc storage profiles found in target provider vdc
            for storageProfile in storagePoliciesFound:
                # iterating over the storage profiles of target provider vdc
                for targetStorageProfile in targetStorageProfiles:
                    if storageProfile['@name'] == targetStorageProfile['@name']:
                        # get api call to retrieve the target pvdc storage profile details
                        getResponse = self.restClientObj.get(targetStorageProfile['@href'], self.headers)
                        if getResponse.status_code == requests.codes.ok:
                            getResponseDict = xmltodict.parse(getResponse.content)
                            if getResponseDict['ProviderVdcStorageProfile']['Enabled'] == "false":
                                targetPVDCDisabledStorageProfiles.append(storageProfile['@name'])
                        else:
                            raise Exception("Failed to retrieve target provider vdc storage profile '{}' information".format(targetStorageProfile['@name']))
                        break

            # if targetPVDCDisabledStorageProfiles is not empty then appending the error message in errorList
            if targetPVDCDisabledStorageProfiles:
                errorList.append("Storage profiles '{}' disabled on target Provider VDC".format(', '.join(targetPVDCDisabledStorageProfiles)))

            # if errorList is not empty then raising all the exception present in the list
            if errorList:
                raise Exception('\n'.join(errorList))
            else:
                logger.debug("Validated successfully, storage Profiles in target PVDC are same as those of source Org VDC")
                logger.debug("Validated successfully, source org vdc storage profiles are all enabled in target provider vdc")

        except Exception:
            raise

    @isSessionExpired
    def validateExternalNetworkSubnets(self):
        """
        Description :  Validate the external networks subnet configuration
        """
        try:
            edgeGatewayUplinksData = []

            for edgeGateway in copy.deepcopy(self.rollback.apiData['sourceEdgeGateway']):
                edgeGatewayUplinksData += edgeGateway['edgeGatewayUplinks']
            # reading the data from metadata
            data = self.rollback.apiData
            # comparing the source and target external network subnet configuration
            if 'sourceExternalNetwork' in data.keys() and 'targetExternalNetwork' in data.keys():
                sourceExternalGatewayAndPrefixList = {(subnet['gateway'], subnet['prefixLength'])for edgeGatewayUplink in edgeGatewayUplinksData for subnet in edgeGatewayUplink['subnets']['values']}
                targetExternalGatewayList = [targetExternalGateway['gateway'] for targetExternalGateway in data['targetExternalNetwork']['subnets']['values']]
                targetExternalPrefixLengthList = [targetExternalGateway['prefixLength'] for targetExternalGateway in data['targetExternalNetwork']['subnets']['values']]
                sourceNetworkAddressList = [ipaddress.ip_network('{}/{}'.format(externalGateway, externalPrefixLength), strict=False)
                                            for externalGateway, externalPrefixLength in sourceExternalGatewayAndPrefixList]
                targetNetworkAddressList = [ipaddress.ip_network('{}/{}'.format(externalGateway, externalPrefixLength), strict=False)
                                            for externalGateway, externalPrefixLength in zip(targetExternalGatewayList, targetExternalPrefixLengthList)]
                if not all(sourceNetworkAddress in targetNetworkAddressList for sourceNetworkAddress in sourceNetworkAddressList):
                    raise Exception('All the Source External Networks Subnets are not present in Target External Network.')
                logger.debug('Validated successfully, all the Source External Networks Subnets are present in Target External Network.')
            else:
                raise Exception('sourceExternalNetwork or targetExternalNetwork not present')
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCAffinityRules(self, orgVDCId):
        """
        Description : Get Org VDC affinity rules
        Parameters :  orgVDCId - org VDC id (STRING)
        """
        try:
            logger.debug("Getting Source Org VDC affinity rules")
            vdcId = orgVDCId.split(':')[-1]
            # url to retrieve org vdc affinity rules
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_AFFINITY_RULES.format(vdcId))
            # get api call to retrieve org vdc affinity rules
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                data = self.rollback.apiData
                data['sourceVMAffinityRules'] = responseDict['VmAffinityRules']['VmAffinityRule'] if responseDict['VmAffinityRules'].get('VmAffinityRule', None) else {}
                logger.debug("Retrieved Source Org VDC affinity rules Successfully")
            else:
                raise Exception("Failed to retrieve VM Affinity rules of source Org VDC")
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCEdgeGateway(self, orgVDCId):
        """
        Description : Gets the list of all Edge Gateways for the specified Organization VDC
        Parameters  : orgVDCId - source Org VDC Id (STRING)
        Returns     : Org VDC edge gateway dict (DICTIONARY)
        """
        try:
            logger.debug("Getting Org VDC Edge Gateway details")
            url = "{}{}?filter=(orgVdc.id=={})".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                       vcdConstants.ALL_EDGE_GATEWAYS, orgVDCId)
            # get api call to retrieve all edge gateways of the specified org vdc
            response = self.restClientObj.get(url, self.headers)
            responseDict = response.json()
            edgeGatewayData = {}
            if response.status_code == requests.codes.ok:
                logger.debug("Org VDC Edge gateway details retrieved successfully.")
                resultTotal = responseDict['resultTotal']
                edgeGatewayData = copy.deepcopy(responseDict)
                edgeGatewayData['values'] = []
            else:
                raise Exception('Failed to retrieve Org VDC Edge gateway details due to: {}'.format(responseDict['message']))
            pageNo = 1
            pageSizeCount = 0
            resultList = []
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}&filter=(orgVdc.id=={})".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.ALL_EDGE_GATEWAYS, pageNo, 15, orgVDCId)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    edgeGatewayData['values'].extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('Org VDC Edge Gateway result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    responseDict = response.json()
                    raise Exception('Failed to get Org VDC Edge Gateway details due to: {}'.format(responseDict['message']))
            logger.debug('Total Org VDC Edge Gateway result count = {}'.format(len(resultList)))
            logger.debug('All Org VDC Edge Gateway successfully retrieved')
            return edgeGatewayData
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCEdgeGatewayId(self, orgVDCId):
        """
        Description :   Get source edge gateway ID's
        Parameters  :   orgVDCId    -   id of the source org vdc (STRING)
        """
        try:
            responseDict = self.getOrgVDCEdgeGateway(orgVDCId)
            logger.debug('Getting the source Edge gateway details')
            data = self.rollback.apiData
            if not responseDict['values']:
                logger.debug("No edge gateway exist in this org vdc")
                data['sourceEdgeGateway'] = []
            else:
                data['sourceEdgeGateway'] = responseDict['values']
            return [value['id'] for value in responseDict['values']]
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayAdminApiDetails(self, edgeGatewayId, staticRouteDetails = None, returnDefaultGateway = False):
        """
            Description :   Get details of edge gateway from admin API
            Parameters  :   edgeGatewayId   -   Edge Gateway ID  (STRING)
                            staticRouteDetails  -   Destails of static routes
                            returnDefaultGateway    -   Flag if default gateway details are to be returned
            Returns     :   Details of edge gateway
        """
        try:
            defaultGatewayDict= dict()
            noSnatList = list()
            allnonDefaultGatewaySubnetList = list()
            logger.debug('Getting Edge Gateway Admin API details')
            url = '{}{}'.format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId))
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                for eachGatewayInterface in responseDict['configuration']['gatewayInterfaces']['gatewayInterface']:
                    for eachSubnetParticipant in eachGatewayInterface['subnetParticipation']:
                        # gather data of default gateway
                        if eachSubnetParticipant['useForDefaultRoute'] == True:
                            defaultGatewayDict['gateway'] = eachSubnetParticipant['gateway']
                            defaultGatewayDict['netmask'] = eachSubnetParticipant['netmask']
                            defaultGatewayDict['subnetPrefixLength'] = eachSubnetParticipant['subnetPrefixLength']
                            defaultGatewayDict['ipRanges'] = list()
                            if eachSubnetParticipant['ipRanges'] is not None:
                                for eachIpRange in eachSubnetParticipant['ipRanges']['ipRange']:
                                    defaultGatewayDict['ipRanges'].append('{}-{}'.format(eachIpRange['startAddress'],
                                                                                         eachIpRange['endAddress']))
                            # if ip range is not present assign ip address as ipRange
                            elif eachSubnetParticipant['ipRanges'] is None:
                                defaultGatewayDict['ipRanges'].append('{}-{}'.format(eachSubnetParticipant['ipAddress'],
                                                                                     eachSubnetParticipant['ipAddress']))
                            else:
                                return ['Failed to get default gateway sub allocated IPs\n']
                        else:
                            if eachGatewayInterface['interfaceType'] == 'uplink':
                                allnonDefaultGatewaySubnetList.extend(eachGatewayInterface['subnetParticipation'])
                            if staticRouteDetails is not None:
                                # if current interface has static routes
                                if eachGatewayInterface['name'] in staticRouteDetails.keys():
                                    noSnatList.append(staticRouteDetails[eachGatewayInterface['name']]['network'])
                if defaultGatewayDict == {} and returnDefaultGateway is True:
                    return ['Default Gateway not configured on Edge Gateway\n']
                if returnDefaultGateway is False and noSnatList is not []:
                    return allnonDefaultGatewaySubnetList, defaultGatewayDict, noSnatList
                else:
                    return defaultGatewayDict
            else:
                return ['Failed to get edge gateway admin api response\n']
        except Exception:
            raise

    @isSessionExpired
    def getEdgesExternalNetworkDetails(self, edgeGatewayId):
        """
            Description :   Get details of all the external networks in a edge gateway
            Parameters  :   edgeGatewayId   -   Edge Gateway ID  (STRING)
            Returns     :   Details of edge interfaces
        """
        try:
            url = '{}{}'.format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                vcdConstants.EDGES_EXTERNAL_NETWORK.format(edgeGatewayId))
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                return response.json()['edgeInterfaces']
            else:
                raise Exception('Failed to retrieve static routing configuration')
        except Exception:
            raise

    @isSessionExpired
    def getStaticRoutesDetails(self, edgeGatewayId):
        """
            Description :   Get details of static routes connected to edge gateway
            Parameters  :   edgeGatewayId   -   Edge Gateway ID  (STRING)
            Returns     :   details of static routes (DICT)
        """
        try:
            allStaticRouteDict = dict()
            logger.debug('Getting static route details')
            url = '{}{}'.format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                vcdConstants.STATIC_ROUTING_CONFIG.format(edgeGatewayId))
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                if responseDict['staticRoutes'] != {}:
                    edgesEternalNetworkList = self.getEdgesExternalNetworkDetails(edgeGatewayId)
                    allStaticRoutes = responseDict['staticRoutes']['staticRoutes']
                    for eachStaticRoute in allStaticRoutes:
                        if eachStaticRoute.get('vnic'):
                            for eachExternalNetworkInEdges in edgesEternalNetworkList:
                                if int(eachStaticRoute['vnic']) == eachExternalNetworkInEdges['index']:
                                    allStaticRouteDict[eachExternalNetworkInEdges['name']] = eachStaticRoute
                                    break
                    return allStaticRouteDict
                else:
                    logger.debug('No static routes present')
                    return None
            else:
                raise Exception('Failed to get static routing configuration')
        except Exception:
            raise

    @isSessionExpired
    def retrieveNetworkListFromMetadata(self, orgVdcId, orgVDCType='source', dfwStatus=False):
        """
            Description :   Gets the details of all the Org VDC Networks as per the status saved in metadata
            Parameters  :   orgVDCId     - source Org VDC Id (STRING)
                            orgVDCType   - type of Org VDC i.e. source/target (STRING)
                            dfwStatus    - True - to make ownerRef False - OrgVDCID
            Returns     :   Org VDC Networks object (LIST)
        """
        networkList = list()
        networkType = 'sourceOrgVDCNetworks' if orgVDCType == 'source' else 'targetOrgVDCNetworks'
        orgVdcNetworkList = self.getOrgVDCNetworks(orgVdcId, networkType, dfwStatus=dfwStatus, saveResponse=False)
        sourceNetworkStatus = self.rollback.apiData[networkType]

        for network in orgVdcNetworkList:
            if network['name'] in sourceNetworkStatus:
                for name, data in sourceNetworkStatus.items():
                    if data['id'] == network['id']:
                        network['subnets']['values'][0]['enabled'] = sourceNetworkStatus[network['name']]['enabled']
                        network['networkType'] = sourceNetworkStatus[network['name']]['networkType']
                        network['connection'] = sourceNetworkStatus[network['name']]['connection']
                        networkList.append(network)
        return networkList

    @isSessionExpired
    def getVCDuuid(self):
        """
        Description : This method return the UUID of vcd
        Returns     : UUID of vCD (STRING)
        """
        logger.debug("Fetching UUID of vCD")
        url = vcdConstants.XML_API_URL.format(self.ipAddress) + "site"
        response = self.restClientObj.get(url, headers=self.headers)
        if response.status_code == requests.codes.ok:
            # Fetching UUID of vCD
            vcdUUID = xmltodict.parse(response.content)["Site"]["@id"]
            return vcdUUID
        else:
            raise Exception("Failed to fetch UUID of vCD")

    @isSessionExpired
    def getOrgVDCNetworks(self, orgVDCId, orgVDCNetworkType, sharedNetwork=False, dfwStatus=False, saveResponse=True):
        """
        Description :   Gets the details of all the Organizational VDC Networks for specific org VDC
        Parameters  :   orgVDCId            - source Org VDC Id (STRING)
                        orgVDCNetworkType   - type of Org VDC Network (STRING)
                        sharedNetwork       - fetch shared networks as well (BOOLEAN)
                        dfwStatus           - status to check ownerref (BOOLEAN)
        Returns     :   Org VDC Networks object (LIST)
        """
        try:
            if float(self.version) <= float(vcdConstants.API_VERSION_PRE_ZEUS):
                key = 'orgVdc'
                urlForNetworks = "{}{}?filter=({}.id=={})"
                urlForNetworksPagenation = "{}{}?page={}&pageSize={}&filter=({}.id=={})"
            elif float(self.version) >= float(vcdConstants.API_VERSION_ZEUS) and sharedNetwork:
                key = 'ownerRef'
                urlForNetworks = "{}{}?filter=(({}.id=={});(_context==includeAccessible))"
                urlForNetworksPagenation = "{}{}?page={}&pageSize={}&filter=(({}.id=={});(_context==includeAccessible))"
            else:
                key = 'ownerRef'
                urlForNetworks = "{}{}?filter=({}.id=={})"
                urlForNetworksPagenation = "{}{}?page={}&pageSize={}&filter=({}.id=={})"

            ownerRefslist = self.rollback.apiData['OrgVDCGroupID'].values() if self.rollback.apiData.get(
                'OrgVDCGroupID') else []

            if dfwStatus and ownerRefslist:
                orgVDCIdList = list(ownerRefslist) + [orgVDCId]
            else:
                orgVDCIdList = [orgVDCId]

            orgVDCNetworkList = list()
            logger.debug("Getting Org VDC network details")

            for orgVDCId in orgVDCIdList:
                # url to retrieve all the org vdc networks of the specified org vdc
                url = urlForNetworks.format(
                    vcdConstants.OPEN_API_URL.format(self.ipAddress),
                    vcdConstants.ALL_ORG_VDC_NETWORKS, key, orgVDCId)
                # get api call to retrieve all the org vdc networks of the specified org vdc
                response = self.restClientObj.get(url, self.headers)
                responseDict = response.json()

                if response.status_code == requests.codes.ok:
                    logger.debug("Retrieved Org VDC Network details successfully")
                    resultTotal = responseDict['resultTotal']
                else:
                    raise Exception('Failed to get Org VDC network details due to: {}'.format(responseDict['message']))

                pageNo = 1
                pageSizeCount = 0
                resultList = []
                logger.debug('Getting Org VDC Networks')
                while resultTotal > 0 and pageSizeCount < resultTotal:
                    url = urlForNetworksPagenation.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                            vcdConstants.ALL_ORG_VDC_NETWORKS, pageNo,
                                                            15, key, orgVDCId)
                    getSession(self)
                    response = self.restClientObj.get(url, self.headers)
                    if response.status_code == requests.codes.ok:
                        responseDict = response.json()
                        resultList.extend(responseDict['values'])
                        pageSizeCount += len(responseDict['values'])
                        logger.debug('Org VDC Networks result pageSize = {}'.format(pageSizeCount))
                        pageNo += 1
                        resultTotal = responseDict['resultTotal']
                    else:
                        responseDict = response.json()
                        raise Exception('Failed to get Org VDC network details due to: {}'.format(responseDict['message']))
                logger.debug('Total Org VDC Networks result count = {}'.format(len(resultList)))
                logger.debug('All Org VDC Networks successfully retrieved')

                for network in resultList:
                    orgVDCNetworkList.append(network)

            if saveResponse:
                networkDataToSave = {}
                for network in orgVDCNetworkList:
                    networkDataToSave[network['name']] = {
                        'id': network['id'],
                        'enabled': network['subnets']['values'][0]['enabled'],
                        'networkType': network['networkType'],
                        'connection': network['connection'],
                        'vdcName': network[key]['name']
                    }
                self.rollback.apiData[orgVDCNetworkType] = networkDataToSave
            return orgVDCNetworkList
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCNetworkDHCPConfig(self, orgVDCNetworksList):
        """
        Description - Get Org VDC Networks DHCP config details
        """
        try:
            allOrgVDCNetworkDHCPList = list()
            if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                dhcpErrorList = list()
                logger.debug('Validating Isolated OrgVDCNetwork DHCP configuration')
                for orgVDCNetwork in orgVDCNetworksList:
                    disabledDhcpPools = bool()
                    tempDhcpPoolList = list()
                    if orgVDCNetwork['networkType'] == 'ISOLATED':
                        eachOrgVDCNetworkDict = dict()
                        url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                            vcdConstants.ORG_VDC_NETWORK_DHCP.format(orgVDCNetwork['id']))
                        response = self.restClientObj.get(url, self.headers)
                        if response.status_code == requests.codes.ok:
                            responseDict = response.json()
                            if responseDict['enabled'] is False:
                                logger.warning('DHCP is disabled on OrgVDC Network: {}'.format(orgVDCNetwork['name']))
                                continue
                            else:
                                if responseDict.get('dhcpPools'):
                                    for eachDhcpPool in responseDict['dhcpPools']:
                                        if eachDhcpPool['enabled'] is False:
                                            disabledDhcpPools = True
                                        else:
                                            tempDhcpPoolList.append(eachDhcpPool)
                                        if int(eachDhcpPool['maxLeaseTime']) > vcdConstants.MAX_LEASE_TIME:
                                            dhcpErrorList.append(orgVDCNetwork['name'])
                                            break
                                    responseDict['dhcpPools'] = tempDhcpPoolList
                                else:
                                    logger.warning("DHCP pools not present on OrgVDC Network: {}".format(orgVDCNetwork['name']))
                        else:
                            raise Exception('Unable to getOrgVDC Network DHCP configuration')
                        eachOrgVDCNetworkDict[orgVDCNetwork['name']] = responseDict
                        allOrgVDCNetworkDHCPList.append(eachOrgVDCNetworkDict)
                    if disabledDhcpPools is True:
                        logger.warning("DHCP pools in OrgVDC network: {} are in disabled state and will not be migrated to target".format(orgVDCNetwork['name']))
                if dhcpErrorList:
                    raise Exception('OrgVDC Network: {} has lease time more than {}.\nReconfigure the original max lease time after migration is complete'.format(dhcpErrorList, vcdConstants.MAX_LEASE_TIME))
                self.rollback.apiData['OrgVDCIsolatedNetworkDHCP'] = allOrgVDCNetworkDHCPList
            else:
                self.rollback.apiData['OrgVDCIsolatedNetworkDHCP'] = allOrgVDCNetworkDHCPList
        except Exception:
            raise

    @isSessionExpired
    def validateDHCPEnabledonIsolatedVdcNetworks(self, orgVdcNetworkList, edgeGatewayList, edgeGatewayDeploymentEdgeCluster, nsxtObj):
        """
        Description : Validate that DHCP is not enabled on isolated Org VDC Network
        Parameters  : orgVdcNetworkList - Org VDC's network list for a specific Org VDC (LIST)
        """
        try:
            logger.debug('Validating whether DHCP is enabled on source Isolated Org VDC network')
            DHCPEnabledList = list()
            # iterating over the org vdc network list
            for orgVdcNetwork in orgVdcNetworkList:
                # checking only for isolated Org VDC Network
                if orgVdcNetwork['networkType'] == 'ISOLATED':
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_ORG_VDC_NETWORKS,
                                           vcdConstants.DHCP_ENABLED_FOR_ORG_VDC_NETWORK_BY_ID.format(orgVdcNetwork['id']))
                    # get api call to retrieve org vdc networks on which dhcp is enabled
                    response = self.restClientObj.get(url, self.headers)
                    if response.status_code == requests.codes.ok:
                        responseDict = response.json()
                        # checking for enabled parameter in response
                        if responseDict['enabled']:
                            DHCPEnabledList.append(orgVdcNetwork['name'])
                            if len(edgeGatewayList) == 0:
                                if edgeGatewayDeploymentEdgeCluster is not None:
                                    logger.debug("DHCP is enabled on source Isolated Org VDC Network. But source edge gateway is not present.Checking edgeGatewayDeploymentEdgeCluster")
                                    self.validateEdgeGatewayDeploymentEdgeCluster(edgeGatewayDeploymentEdgeCluster, nsxtObj)
                                else:
                                    raise Exception("DHCP is enabled on source Isolated Org VDC Network, but neither Source EdgeGateway is present nor 'EdgeGatewayDeploymentEdgeCluster' is provided in the input file.")
                        else:
                            logger.debug("Validated Successfully, DHCP is not enabled on Isolated Org VDC Network.")
                    else:
                        responseDict = response.json()
                        raise Exception('Failed to fetch DHCP details from Isolated network due to {}'.format
                                        (responseDict['message']))
            if len(DHCPEnabledList) > 0:
                logger.debug("DHCP is enabled on Isolated Org VDC Network: '{}'".format(', '.join(DHCPEnabledList)))

            if (DHCPEnabledList and float(self.version) <= float(vcdConstants.API_VERSION_PRE_ZEUS)):
                raise Exception(
                    "DHCP is not supported with API version 34.0 but is enabled on source Isolated Org VDC Network - {}".format(','.join(DHCPEnabledList))
                )
        except Exception:
            raise

    @isSessionExpired
    def validateOrgVDCNetworkShared(self, sourceOrgVDCId):
        """
        Description :   Validates if Org VDC Networks are not Shared
        Parameters  :   sourceOrgVDCId   -   ID of source org vdc (STRING)
        """
        try:
            # Shared networks are supported starting from Andromeda build
            if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA):
                return

            # iterating over the org vdc networks
            orgVdcNetworkSharedList = list()
            for orgVdcNetwork in self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False, sharedNetwork=True):
                # checking only for isolated Org VDC Network
                if bool(orgVdcNetwork['shared']):
                    orgVdcNetworkSharedList.append(orgVdcNetwork['name'])
            if orgVdcNetworkSharedList:
                raise Exception("Org VDC Network {} is a shared network. No shared networks should exist.".format(','.join(orgVdcNetworkSharedList)))
            else:
                logger.debug("Validated Successfully, No Source Org VDC Networks are shared")
        except Exception:
            raise

    @isSessionExpired
    def validateOrgVDCNetworkDirect(self, orgVdcNetworkList, nsxtProviderVDCName, transportZone, nsxtObj):
        """
        Description :   Validates if Source Org VDC Networks are not direct networks
        Parameters  :   orgVdcNetworkList   -   list of org vdc network list (LIST)
                        nsxtProviderVDCName - Name of NSX-T PVDC
        """
        try:
            orgVdcNetworkDirectList = list()
            errorlist = list()
            for orgVdcNetwork in orgVdcNetworkList:
                if orgVdcNetwork['networkType'] == 'DIRECT':
                    parentNetworkId = orgVdcNetwork['parentNetworkId']
                    networkName, exception = self.validateExternalNetworkdvpg(parentNetworkId, nsxtProviderVDCName, orgVdcNetwork['name'], orgVdcNetwork)
                    if networkName:
                        orgVdcNetworkDirectList.append(networkName)
                    if exception:
                        errorlist.append(exception)
            if orgVdcNetworkDirectList:
                logger.info('Validating Transport Zone {} present in the NSX-T'.format(transportZone))
                exception = nsxtObj.validateDirectNetworkTZ(transportZone)
                if exception:
                    errorlist.append(exception)
            if orgVdcNetworkDirectList and float(self.version) <= float(vcdConstants.API_VERSION_ZEUS):
                raise Exception("Direct network {} exist in source Org VDC. Direct networks can't be migrated to target Org VDC".format(','.join(orgVdcNetworkDirectList)))
            elif float(self.version) <= float(vcdConstants.API_VERSION_ZEUS):
                logger.debug("Validated Successfully, No direct networks exist in Source Org VDC")
            if errorlist:
                raise Exception(''.join(errorlist))
        except Exception:
            raise

    @isSessionExpired
    def getSourceExternalNetworkName(self, edgeGatewayIdList):
        """
            Description :   Fetch name of source external networks
            Parameters  :   edgeGatewayIdList   -   List of Id's of the Edge Gateway  (STRING)
        """
        sourceExternalNetworks = []
        for sourceEdgeGatewayId in edgeGatewayIdList:
            edgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId))
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # retrieving the details of the edge gateway
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                for gatewayInterface in responseDict['configuration']['gatewayInterfaces']['gatewayInterface']:
                    if gatewayInterface['interfaceType'] == 'uplink':
                        sourceExternalNetworks.append(gatewayInterface['name'])
        return set(sourceExternalNetworks)

    @isSessionExpired
    def getServiceGroups(self, orgVdcId):
        """
            Description :   Fetch name of source external networks
            Parameters  :   orgVdcId   -   OrgVDC Id in URN format  (STRING)
        """
        try:
            allServiceGroupsList = list()
            logger.debug('Getting details of Application services group')
            orgVdcIdStr = orgVdcId.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                            vcdConstants.GET_APPLICATION_SERVICE_GROUPS.format(orgVdcIdStr))
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                for eachServiceGroup in responseDict['list']['applicationGroup']:
                    allServiceGroupsList.append(eachServiceGroup['name'])
                return allServiceGroupsList
            else:
                logger.error('Failed to get application services group details')
                raise Exception('Failed to get application services group details')
        except Exception:
            raise

    @isSessionExpired
    def validateEdgeGatewayRateLimit(self, edgeGatewayIdList):
        """
            Description :   Validate Edge Gateway uplinks
            Parameters  :   edgeGatewayIdList   -   List of Id's of the Edge Gateway  (STRING)
        """
        try:
            for sourceEdgeGatewayId in edgeGatewayIdList:
                edgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId))
                acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
                headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
                # retrieving the details of the edge gateway
                response = self.restClientObj.get(url, headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    gatewayInterfaces = responseDict['configuration']['gatewayInterfaces']['gatewayInterface']
                    # checking whether source edge gateway has rate limit configured
                    rateLimitEnabledInterfaces = [interface for interface in gatewayInterfaces if
                                                  interface.get('applyRateLimit', None)]
                    if rateLimitEnabledInterfaces:
                        raise Exception("Rate Limit is configured on edge gateway")
        except Exception:
            raise


    @isSessionExpired
    def validateEdgeGatewayUplinks(self, sourceOrgVDCId, edgeGatewayIdList):
        """
            Description :   Validate Edge Gateway uplinks
            Parameters  :   edgeGatewayIdList   -   List of Id's of the Edge Gateway  (STRING)
        """
        try:
            # fetching network list
            errorList = list()
            sourceOrgVDCNetworks = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)
            for sourceEdgeGatewayId in edgeGatewayIdList:
                edgeGatewayId = sourceEdgeGatewayId.split(':')[-1]

                # Filtering networks connected to edge gateway
                networkList = list(filter(lambda network: network.get('connection') and
                                          network.get('connection', {}).get('routerRef', {})
                                          .get('id') == sourceEdgeGatewayId and network
                                          .get('networkType') == 'NAT_ROUTED', sourceOrgVDCNetworks))
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId))
                acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
                headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
                # retrieving the details of the edge gateway
                response = self.restClientObj.get(url, headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    gatewayInterfaces = responseDict['configuration']['gatewayInterfaces']['gatewayInterface']
                    if len(gatewayInterfaces) > 9 and not networkList:
                        errorList.append('No more uplinks present on source Edge Gateway to connect dummy External Uplink')
                    # checking whether source edge gateway has rate limit configured
                    rateLimitEnabledInterfaces = [interface for interface in gatewayInterfaces if interface['applyRateLimit']]
                    for rateLimitEnabledInterface in rateLimitEnabledInterfaces:
                        logger.info(f"Validating whether source Org VDC Edge Gateway {responseDict['name']} has rate limit configured")
                        logger.warning(f"The source Org VDC Edge Gateway {responseDict['name']} has rate limit configured."
                                       f" External Network {rateLimitEnabledInterface['name']} Incoming {rateLimitEnabledInterface['inRateLimit']} Mbps,"
                                       f" Outgoing {rateLimitEnabledInterface['outRateLimit']} Mbps. "
                                       "After migration apply equivalent Gateway QOS Profile "
                                       "to Tier-1 GW backing the target Org VDC Edge Gateway directly in NSX-T.")
                else:
                    errorList.append('Failed to get Edge Gateway:{} Uplink details'.format(edgeGatewayId))
            if errorList:
                raise Exception(',\n'.join(errorList))
        except Exception:
            raise

    @isSessionExpired
    def _checkDistrbutedFirewallRuleObjectType(self, ruleList, orgVdcId, allSecurityGroups, v2tAssessment=False):
        """
            Description :   validate distributed firewall rules
            Parameters  :   ruleList   -   List of DFW rules  (LIST)
        """
        try:
            errorList = list()
            securityGroupErrors = list()
            InvalidRuleDict = defaultdict(set)
            InvalidSecurityGroupDict = defaultdict(set)
            layer3AppServicesList = list()
            # get layer3 services on source
            allAppServices = self.getApplicationServicesDetails(orgVdcId)

            #get all layer3 servicesBGP service is disabled
            for eachAppService in allAppServices:
                if eachAppService['layer'] == 'layer3':
                    layer3AppServicesList.append(eachAppService['name'])

            if not v2tAssessment:
                # get layer7 supported services
                allNetworkContextProfilesDict = self.getNetworkContextProfiles()

            # get service groups details
            serviceGroupsList = self.getServiceGroups(orgVdcId)
            for eachRule in ruleList:
                l3ServiceCnt = 0
                l7ServiceCnt = 0
                allSources = allDestinations = list()

                if not eachRule.get('name'):
                    key = f"rule_id_{eachRule['@id']}"
                    InvalidRuleDict[key].add('Rule name not present')
                elif eachRule.get('name') and eachRule['name'] == '':
                    key = f"rule_id_{eachRule['@id']}"
                    InvalidRuleDict[key].add('Rule name not present')
                else:
                    key = eachRule['name']

                if eachRule.get('sources'):
                    allSources = eachRule['sources']['source'] if isinstance(eachRule['sources']['source'], list) else [eachRule['sources']['source']]
                if eachRule.get('destinations'):
                    allDestinations = eachRule['destinations']['destination'] if isinstance(eachRule['destinations']['destination'], list) else [eachRule['destinations']['destination']]

                for eachObject in allSources+allDestinations:
                    if v2tAssessment or float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA):
                        if eachObject['type'] not in vcdConstants.DISTRIBUTED_FIREWALL_OBJECT_LIST_ANDROMEDA:
                            InvalidRuleDict[key].add(eachObject['type'])
                        elif eachObject['type'] == 'SecurityGroup':
                            errors = self.validateSecurityGroupObject(allSecurityGroups[eachObject['value']])
                            if errors:
                                InvalidSecurityGroupDict[key].add(f"Security Group ({allSecurityGroups[eachObject['value']]['name']})")
                                if isinstance(errors, list):
                                    securityGroupErrors.extend(errors)

                    # For versions before Andromeda
                    elif eachObject['type'] not in vcdConstants.DISTRIBUTED_FIREWALL_OBJECT_LIST:
                        InvalidRuleDict[key].add(eachObject['type'])

                if eachRule.get('services'):
                    allServicesInRule = eachRule['services']['service'] \
                        if isinstance(eachRule['services']['service'], list) \
                        else [eachRule['services']['service']]
                    for eachRuleService in allServicesInRule:
                        withAppFlag = bool()
                        if eachRuleService.get('name'):
                            if eachRuleService['name'] in serviceGroupsList:
                                InvalidRuleDict[key].add('{}: {}'.format(eachRuleService['name'], 'is a service group'))
                                continue
                            if eachRuleService['name'] not in layer3AppServicesList:
                                if eachRuleService['name'].startswith('APP_'):
                                    eachRuleService['name'] = eachRuleService['name'][len('APP_'):]
                                    if eachRuleService['name'] == 'KASPERSKY':
                                        eachRuleService['name'] = 'KASPRSKY'
                                    withAppFlag = True

                                if not v2tAssessment:
                                    # compare service name with names of all L7 service
                                    if eachRuleService['name'] not in allNetworkContextProfilesDict.keys():
                                        if withAppFlag is True:
                                            InvalidRuleDict[key].add('{}{}: {}'.format('APP_', eachRuleService['name'], 'not present'))
                                        else:
                                            InvalidRuleDict[key].add('{}: {}'.format(eachRuleService['name'], 'not present'))
                                    else:
                                        l7ServiceCnt += 1
                            else:
                                l3ServiceCnt += 1
                        else:
                            if eachRuleService['protocolName'] == 'TCP' or eachRuleService['protocolName'] == 'UDP':
                                if not eachRuleService.get('sourcePort') or not eachRuleService.get('destinationPort'):
                                    InvalidRuleDict[key].add("{}:{}".format(eachRuleService['protocolName'], 'Any'))
                                else:
                                    l3ServiceCnt += 1
                        # if l3ServiceCnt >= 1 and l7ServiceCnt > 1:
                        #     msg = 'More than one Layer7 service present along with one or more layer3 service'
                        #     if msg not in InvalidRuleDict[eachRule['name']]:
                        #         InvalidRuleDict[eachRule['name']].append(msg)

            for rule, objList in InvalidRuleDict.items():
                errorList.append('Rule: {} has invalid objects: {}'.format(rule, ', '.join(objList)))

            for rule, objList in InvalidSecurityGroupDict.items():
                errorList.append('Rule: {} has invalid security group objects: {}'.format(rule, ', '.join(objList)))

            return errorList + securityGroupErrors

        except Exception:
            raise

    @staticmethod
    def validateSecurityGroupObject(securityGroup):
        """
        Description :   Validates security group
        Parameters  :   securityGroup   -  Security group object (DICT)
        Returns     :   Validation errors on provided security group
        """
        if securityGroup.get('isValidated'):
            return True

        errors = list()
        if securityGroup.get('excludeMember'):
            errors.append(f"Security Group ({securityGroup['name']}): 'Exclude Members' not supported")

        if securityGroup.get('member'):
            includeMembers = (
                securityGroup['member']
                if isinstance(securityGroup['member'], list)
                else [securityGroup['member']])
            for member in includeMembers:
                if member['type']['typeName'] not in ['VirtualMachine', 'SecurityTag', 'IPSet', 'Network']:
                    errors.append(
                        f"Security Group ({securityGroup['name']}): {member['type']['typeName']} not supported in "
                        f"'Include Members'")

        if securityGroup.get('dynamicMemberDefinition'):
            dynamicSets = (
                securityGroup['dynamicMemberDefinition']['dynamicSet']
                if isinstance(securityGroup['dynamicMemberDefinition']['dynamicSet'], list)
                else [securityGroup['dynamicMemberDefinition']['dynamicSet']])

            for setId, dynset in enumerate(dynamicSets):
                setId = setId+1
                criteriaPrefix = f"Security Group ({securityGroup['name']}) - Criteria ({setId}):"
                if dynset['operator'] == 'AND':
                    errors.append(f"{criteriaPrefix} 'AND' operation is not supported")

                dynamicCriteria = dynset['dynamicCriteria'] if isinstance(dynset['dynamicCriteria'], list) else [dynset['dynamicCriteria']]
                hasOR = False
                for ruleId, rule in enumerate(dynamicCriteria):
                    ruleId = ruleId+1
                    rulePrefix = f"Security Group ({securityGroup['name']}) - Criteria ({setId}) - Rule ({ruleId}):"
                    if rule['operator'] == 'OR':
                        hasOR = True

                    if rule['key'] not in ['VM.NAME', 'VM.SECURITY_TAG']:
                        key = "'VM Guest OS Name'" if rule['key'] == 'VM.GUEST_OS_FULL_NAME' else rule['key']
                        errors.append(f"{rulePrefix} {key} is not supported")
                    elif rule['key'] == 'VM.NAME' and rule['criteria'] not in ['contains', 'starts_with']:
                        errors.append(
                            f"{rulePrefix} {rule['criteria']} is not supported with {rule['key']}")
                    elif rule['key'] == 'VM.SECURITY_TAG' and rule['criteria'] not in [
                            'contains', 'starts_with', 'ends_with']:
                        errors.append(
                            f"{rulePrefix} {rule['criteria']} is not supported with {rule['key']}")

                if len(dynamicCriteria) > 4:
                    errors.append(f"{criteriaPrefix} At most four rules are supported")
                if hasOR:
                    errors.append(f"{criteriaPrefix} 'Match Any' condition is not supported")

        securityGroup['isValidated'] = errors
        return errors

    @isSessionExpired
    def getNetworkContextProfiles(self):
        """
            Description :   Get all the network context profiles
            Returns     :   All layer7 services
        """
        try:
            logger.debug('Getting all network context profiles')
            allNetContextProfiles = dict()
            pageNo = 1
            resultCnt = 0
            # fetching name of NSX-T backed provider vdc
            tpvdcName = self.rollback.apiData['targetProviderVDC']['@name']

            # fetching NSX-T manager id
            nsxtManagerId = self.getNsxtManagerId(tpvdcName)

            while True:
                url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),'networkContextProfiles?page={}&pageSize=25&filter=_context=={}'.format(str(pageNo), nsxtManagerId))
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    if isinstance(responseDict['values'], list) and responseDict['values'] != []:
                        for eachVal in responseDict['values']:
                            allNetContextProfiles[eachVal['name']] = eachVal
                            resultCnt += 1
                        pageNo += 1
                    if responseDict['resultTotal'] == resultCnt:
                        break
                else:
                    logger.error('Failed to get Network  context profiles')
            return allNetContextProfiles
        except Exception:
             raise

    @isSessionExpired
    def getApplicationServicesDetails(self, orgVdcId):
        """
            Description :   Get all the application services
            Parameters  :   orgVdcId   -   OrgVDC ID  (STRING)
            Returns     :   All layer3 application services
        """
        try:
            allLayer3AppServicesDict = dict()
            logger.debug('Getting details of Application services')
            orgVdcIdStr = orgVdcId.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                            vcdConstants.GET_APPLICATION_SERVICES.format(orgVdcIdStr))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)

                return responseDict['list']['application']
            else:
                logger.error('Failed to get application services details')
                raise Exception("Failed to get application services details")
        except Exception:
            raise

    @isSessionExpired
    def getDistributedFirewallConfig(self, orgVdcId, validation=False, validateRules=True, v2tAssessmentMode=False):
        """
            Description :   Get DFW configuration
            Parameters  :   orgVdcId   -   OrgVDC ID  (STRING)
            Returns     :   List of all the exceptions
        """
        if not validation and self.l3DfwRules is not None:
            return self.l3DfwRules

        try:
            logger.debug("Getting Org VDC Distributed Firewall details")
            allErrorList = list()
            orgVdcIdStr = orgVdcId.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                            vcdConstants.GET_DISTRIBUTED_FIREWALL.format(orgVdcIdStr))
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                if not v2tAssessmentMode and float(self.version) <= float(vcdConstants.API_VERSION_PRE_ZEUS):
                    raise Exception('DFW feature is not available in API version 34.0')

                if responseDict['firewallConfiguration']['layer3Sections']['section'].get('rule'):
                    allLayer3Rules = responseDict['firewallConfiguration']['layer3Sections']['section']['rule'] \
                        if isinstance(responseDict['firewallConfiguration']['layer3Sections']['section']['rule'], list) \
                        else [responseDict['firewallConfiguration']['layer3Sections']['section']['rule']]

                    allSecurityGroups = self.getSourceDfwSecurityGroups()

                    if validateRules:
                        allErrorList = self._checkDistrbutedFirewallRuleObjectType(
                            allLayer3Rules, orgVdcId, allSecurityGroups, v2tAssessment=v2tAssessmentMode)

                    if validation:
                        # get all the id's of conflict networks
                        conflictIDs = self.networkConflictValidation(orgVdcId)
                        # get the exception if a conflict network is used in DFW
                        conflictNetworks = self.validatingDFWobjects(orgVdcId, allLayer3Rules, conflictIDs, allSecurityGroups)
                        # adding the the exception to the list
                        allErrorList.extend(conflictNetworks)

                if responseDict['firewallConfiguration']['layer2Sections']['section'].get('rule'):
                    if isinstance(responseDict['firewallConfiguration']['layer2Sections']['section']['rule'], dict) and\
                            responseDict['firewallConfiguration']['layer2Sections']['section']['rule']['name'] == 'Default Allow Rule':
                        logger.debug('Default layer2 rule present')
                    else:
                        allErrorList.append('Layer2 rule present in distributed firewall')
                else:
                    logger.debug('Layer2 rules are not present in distributed firewall')

                if v2tAssessmentMode:
                    return allErrorList

                if allErrorList:
                    raise Exception(',\n'.join(allErrorList))
                else:
                    self.l3DfwRules = responseDict['firewallConfiguration']['layer3Sections']['section']['rule']
                    return self.l3DfwRules

            elif response.status_code == 400:
                logger.debug('Distributed Firewall is disabled')
                self.l3DfwRules = []
                return self.l3DfwRules
            else:
                raise Exception('Failed to get status of distributed firewall config')
        except Exception:
            raise

    def getDistributedFirewallRules(self, orgVdcId, ruleType='all', validateRules=True):
        """
        Description :   Get DFW rules specified by ruleType parameter.
                        'all' - It will return all rules without filter
                        'default' - It will return only default rule
                        'non-default' - It will return all rules except default rule
        Parameters  :   orgVdcId    - Org VDC ID (STR)
                        ruleType    - Type of rule to be fetched (One of: 'all', 'default', 'non-default') (STR)

        Returns     :   DFW firewall rules
        """
        rules = self.getDistributedFirewallConfig(orgVdcId, validateRules=validateRules)
        if ruleType == 'all':
            return rules

        if rules:
            rules = rules if isinstance(rules, list) else [rules]

            # If last rule is any-any-any(source-destination-service), consider it as default rule and
            # separate if from rules list
            lastRule = rules[-1]
            if not lastRule.get('sources') and not lastRule.get('destinations') and not lastRule.get('services'):
                defaultRule = lastRule
                rules = rules[:-1]
            else:
                defaultRule = {}
        else:
            raise DfwRulesAbsentError('DFW rules not present')

        if ruleType == 'default':
            return defaultRule

        if ruleType == 'non-default':
            return rules

        raise Exception('Invalid ruleType parameter provided')

    def validateSnatRuleOnDefaultGateway(self, defaultGatewayDetails, natRule):
        """
            Description :   Gets the Edge gateway services Configuration details
            Parameters  :   defaultGatewayDetails - default gateway details
                            natRule - individual nat rule
        """
        try:
            for eachIpRange in defaultGatewayDetails['ipRanges']:
                startIpAddr, endIpAddr = eachIpRange.split('-')
                if self.ifIpBelongsToIpRange(natRule['translatedAddress'], startIpAddr, endIpAddr) == True:
                    return True
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayServices(self, nsxtObj=None, nsxvObj=None, noSnatDestSubnetAddr=None, preCheckMode=False, ServiceEngineGroupName=None, v2tAssessmentMode=False):
        """
        Description :   Gets the Edge gateway services Configuration details
        Parameters  :   nsxtObj - nsxtOperations class object
                        noSnatDestSubnetAddr    -   NoSNAT destination subnet from sample input
                        preCheckMode    -   if migrator tool is run in preCheck mode (BOOLEAN)
                        ServiceEngineGroupName - Name of service engine group for load balancer configuration (STRING)
                        v2tAssessmentMode - bool the sets whether v2tAssessmentMode is executing this method or not (BOOLEAN)
        """
        try:
            logger.info('Getting the services configured on source Edge Gateway')

            # Handle condition if NSX-IP if different than the one registered in vCD
            if not v2tAssessmentMode and not self.nsxVersion:
                raise Exception('Incorrect NSX-T IP Address in input file. '
                        'Please check if the NSX-T IP Address matches the one in NSXT-Managers in vCD')

            errorData = {'DHCP': [],
                         'Firewall': [],
                         'NAT': [],
                         'IPsec': [],
                         'BGP': [],
                         'Routing': [],
                         'LoadBalancer': [],
                         'L2VPN': [],
                         'SSLVPN': [],
                         'DNS': []}
            self.rollback.apiData['sourceEdgeGatewayDHCP'] = {}
            ipsecConfigDict = {}
            allErrorList = list()
            edgeGatewayCount = 0
            for edgeGateway in self.rollback.apiData['sourceEdgeGateway']:
                currentErrorList = list()
                gatewayId = edgeGateway['id'].split(':')[-1]
                gatewayName = edgeGateway['name']
                currentErrorList.append("Edge Gateway: " + gatewayName + '\n')
                logger.debug('Getting the services configured on source Edge Gateway - {}'.format(gatewayName))

                # getting the dhcp config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayDhcpConfig, gatewayId, v2tAssessmentMode=v2tAssessmentMode)
                time.sleep(2)
                # getting the firewall config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayFirewallConfig, gatewayId)
                time.sleep(2)
                # getting the nat config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayNatConfig, gatewayId)
                time.sleep(2)
                # getting the ipsec config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayIpsecConfig, gatewayId)
                time.sleep(2)
                # getting the bgp config details of specified edge gateway
                self.thread.spawnThread(self.getEdgegatewayBGPconfig, gatewayId, validation=True, nsxtObj=nsxtObj, v2tAssessmentMode=v2tAssessmentMode)
                time.sleep(2)
                # getting the routing config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayRoutingConfig, gatewayId, precheck=preCheckMode)
                time.sleep(2)
                # getting the load balancer config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayLoadBalancerConfig, gatewayId, ServiceEngineGroupName, nsxvObj=nsxvObj, v2tAssessmentMode=v2tAssessmentMode)
                time.sleep(2)
                # getting the l2vpn config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayL2VPNConfig, gatewayId)
                time.sleep(2)
                # getting the sslvpn config details of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewaySSLVPNConfig, gatewayId)
                time.sleep(2)
                # getting the dns config of specified edge gateway
                self.thread.spawnThread(self.getEdgeGatewayDnsConfig, gatewayId)
                time.sleep(2)

                # Halting the main thread till all the threads have completed their execution
                self.thread.joinThreads()

                # Fetching saved values from thread class of all the threads
                dhcpErrorList, dhcpConfigOut = self.thread.returnValues['getEdgeGatewayDhcpConfig']
                firewallErrorList = self.thread.returnValues['getEdgeGatewayFirewallConfig']
                natErrorList, ifNatRulesPresent = self.thread.returnValues['getEdgeGatewayNatConfig']
                ipsecErrorList, ipsecConfigOut = self.thread.returnValues['getEdgeGatewayIpsecConfig']
                bgpErrorList, bgpStatus = self.thread.returnValues['getEdgegatewayBGPconfig']
                routingErrorList, routingDetails = self.thread.returnValues['getEdgeGatewayRoutingConfig']
                loadBalancingErrorList = self.thread.returnValues['getEdgeGatewayLoadBalancerConfig']
                L2VpnErrorList = self.thread.returnValues['getEdgeGatewayL2VPNConfig']
                SslVpnErrorList = self.thread.returnValues['getEdgeGatewaySSLVPNConfig']
                dnsErrorList = self.thread.returnValues['getEdgeGatewayDnsConfig']
                if bgpStatus is True and edgeGatewayCount > 1:
                    bgpErrorList.append('BGP is enabled on: {} and more than 1 edge gateway present'.format(gatewayName))
                currentErrorList = currentErrorList + dhcpErrorList + firewallErrorList + natErrorList + ipsecErrorList \
                               + bgpErrorList + routingErrorList + loadBalancingErrorList + L2VpnErrorList \
                               + SslVpnErrorList + dnsErrorList
                defaultGatewayDetails = self.getEdgeGatewayAdminApiDetails(gatewayId, returnDefaultGateway=True)
                if isinstance(defaultGatewayDetails, list):
                    currentErrorList = currentErrorList + defaultGatewayDetails
                if len(currentErrorList) > 1:
                    allErrorList = allErrorList + currentErrorList
                if preCheckMode is False and isinstance(defaultGatewayDetails, dict):
                    ifRouterIdInDefaultGateway = False
                    ifSnatOnDefaultGateway = False
                    natRules = ifNatRulesPresent if isinstance(ifNatRulesPresent, list) else []
                    if natRules != [] and defaultGatewayDetails != {}:
                        for eachNatRule in natRules:
                            if self.validateSnatRuleOnDefaultGateway(defaultGatewayDetails, eachNatRule) == True:
                                ifSnatOnDefaultGateway = True
                                break
                    else:
                        logger.debug('NAT rules not present on default gateway or default gateway on Edge Gateway is disabled')
                        ifSnatOnDefaultGateway = False
                    if defaultGatewayDetails.get('ipRanges') and routingDetails is not None and \
                            routingDetails['routingGlobalConfig'].get('routerId'):
                        for eachIpRange in defaultGatewayDetails['ipRanges']:
                            startIpAddr, endIpAddr = eachIpRange.split('-')
                            # check if routerId in dynamic routing config part of default gateway IP range
                            if self.ifIpBelongsToIpRange(routingDetails['routingGlobalConfig']['routerId'], startIpAddr, endIpAddr) == True:
                                ifRouterIdInDefaultGateway = True
                                break
                    else:
                        logger.debug('Either default gateway id disabled or Router is not configured')
                    if ifRouterIdInDefaultGateway is False and noSnatDestSubnetAddr is None and \
                            ifSnatOnDefaultGateway is True and bgpStatus is True:
                        logger.warning('BGP learnt routes route via non-default GW external interface present but NoSnatDestinationSubnet is not configured. For each SNAT rule on the default GW interface SNAT rule will be created')
                    self.rollback.apiData['sourceEdgeGatewayDHCP'][edgeGateway['id']] = dhcpConfigOut
                    logger.debug("Source Edge Gateway - {} services configuration retrieved successfully".format(gatewayName))
                    ipsecConfigDict[gatewayName] = ipsecConfigOut


                errorData['DHCP'] = errorData.get('DHCP', []) + dhcpErrorList
                errorData['Firewall'] = errorData.get('Firewall', []) + firewallErrorList
                errorData['NAT'] = errorData.get('NAT', []) + natErrorList
                errorData['IPsec'] = errorData.get('IPsec', []) + ipsecErrorList
                errorData['BGP'] = errorData.get('BGP', []) + bgpErrorList
                errorData['Routing'] = errorData.get('Routing', []) + routingErrorList
                errorData['LoadBalancer'] = errorData.get('LoadBalancer', []) + loadBalancingErrorList
                errorData['L2VPN'] = errorData.get('L2VPN', []) + L2VpnErrorList
                errorData['SSLVPN'] = errorData.get('SSLVPN', []) + SslVpnErrorList
                errorData['DNS'] = errorData.get('DNS', []) + dnsErrorList
            if v2tAssessmentMode:
                return errorData
            if allErrorList:
                raise Exception(''.join(allErrorList))
            return ipsecConfigDict
        except Exception:
            raise

    @isSessionExpired
    def getNamedDiskInOrgVDC(self, orgVDCId, orgId=None):
        """
        Description :   Gets the list of named disks in a Org VDC
        Parameters  :   orgVDCId - ID of org VDC (STRING)
        Returns     :   List of disk with details (LIST)
        """
        try:
            logger.debug('Getting Named Disks present in Org VDC')
            orgVDCIdShort = orgVDCId.split(':')[-1]
            if orgVDCId in self.namedDisks:
                return

            orgId = orgId or self.rollback.apiData['Organization']['@id']
            pageSize = vcdConstants.DEFAULT_QUERY_PAGE_SIZE
            base_url = "{}{}".format(
                vcdConstants.XML_API_URL.format(self.ipAddress),
                vcdConstants.GET_NAMED_DISK_BY_VDC.format(orgVDCIdShort))
            # Get first page of query
            pageNo = 1
            url = f"{base_url}&page={pageNo}&pageSize={pageSize}&format=records"
            headers = {
                'Authorization': self.headers['Authorization'],
                'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE,
                'X-VMWARE-VCLOUD-TENANT-CONTEXT': orgId.split(':')[-1],
            }
            response = self.restClientObj.get(url, headers)
            if not response.status_code == requests.codes.ok:
                raise Exception(f'Error occurred while retrieving named disks details: {response.json()["message"]}')

            # Store first page result and preapre for second page
            responseContent = response.json()
            resultTotal = responseContent['total']
            resultFetched = responseContent['record']
            pageNo += 1

            # Return if results are empty
            if resultTotal == 0:
                self.namedDisks[orgVDCId] = []
                return

            # Query second page onwards until resultTotal is reached
            while len(resultFetched) < resultTotal:
                url = f"{base_url}&page={pageNo}&pageSize={pageSize}&format=records"
                getSession(self)
                response = self.restClientObj.get(url, headers)
                responseContent = response.json()
                if not response.status_code == requests.codes.ok:
                    raise Exception(
                        f'Error occurred while retrieving named disks details: {responseContent["message"]}')

                resultFetched.extend(responseContent['record'])
                resultTotal = responseContent['total']
                logger.debug(f'named disks details result pageSize = {len(resultFetched)}')
                pageNo += 1

            logger.debug(f'Total named disks details result count = {len(resultFetched)}')
            logger.debug(f'named disks details successfully retrieved')

            for disk in resultFetched:
                disk['id'] = disk['href'].split('/')[-1]

            # Save only selected parameters
            self.namedDisks[orgVDCId] = [
                {
                    param: disk.get(param)
                    for param in ['id', 'name', 'iops', 'storageProfile', 'storageProfileName', 'isAttached',
                                  'href', 'isShareable', 'sharingType']
                }
                for disk in resultFetched
            ]

        except Exception as e:
            logger.error(f'Error occurred while retrieving Named Disks: {e}')
            raise

    @isSessionExpired
    def getAttachedVms(self, disk):
        """
        Description :   Get list of VMs attached to disk
        Parameters  :   disk    -   disk details fetched using disk/{id} API (DICT)
        Returns     :   List of VMs in VmsType output (LIST)
        """
        url = f"{disk['href']}/{vcdConstants.GET_ATTACHED_VMS_TO_DISK}"
        try:
            headers = {
                'Authorization': self.headers['Authorization'],
                'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE.format(self.version),
            }
            response = self.restClientObj.get(url, headers)

            if not response.status_code == requests.codes.ok:
                logger.error(f'Error occurred while retrieving entity details: {response.json()["message"]}')
                raise Exception(f'Error occurred while retrieving entity details: {response.json()["message"]}')

            vms = response.json().get('vmReference')

            if not vms:
                return
            # As shared disks are not supported, we will get only one result for attached vm
            return vms[0]

        except Exception as e:
            logger.error(f'Error occurred while retrieving VMs attached to Disk - {disk["name"]}: {e}')
            raise

    @isSessionExpired
    def getVm(self, url):
        """
        Description :   Get details of entity in json format
        Parameters  :   url    -   url of entity (STRING)
        Returns     :   response.json() of url
        """
        headers = {
            'Authorization': self.headers['Authorization'],
            'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE.format(self.version),
        }
        response = self.restClientObj.get(url, headers)

        if response.status_code == requests.codes.ok:
            return response.json()
        else:
            logger.error(f'Error occurred while retrieving VM details: {response.json()["message"]}')
            raise Exception(f'Error occurred while retrieving VM details: {response.json()["message"]}')

    def validateIndependentDisks(self, sourceOrgVDCId, orgId=None, v2tAssessmentMode=False):
        """
        Description :   Validates if the Independent disks in Org VDC
                        For versions before Andromeda, raise exception if named disks are present
                        For versions from Andromeda, raise exception when named disks are shared or attached VM is not powered off.
        Parameters  :   orgVDCId    -   Id of the Org VDC (STRING)
        """
        self.getNamedDiskInOrgVDC(sourceOrgVDCId, orgId)

        if not v2tAssessmentMode and float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA):
            if self.namedDisks.get(sourceOrgVDCId):
                raise Exception("Independent Disks: {} Exist In Source Org VDC.".format(
                    ','.join(disk['name'] for disk in self.namedDisks[sourceOrgVDCId])))
            else:
                logger.debug("Validated Successfully, Independent Disks do not exist in Source Org VDC")
            return

        errors = []
        if not self.namedDisks.get(sourceOrgVDCId):
            if v2tAssessmentMode:
                return errors
            return

        shared_disks = []
        disksWithoutPoweredOffVms = []
        for disk in self.namedDisks[sourceOrgVDCId]:
            # Applicable in assessment mode only. for pre Andromeda version 'isShareable' is applicable
            # to identify shared disk. From andromeda it is 'sharingType'
            if float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA):
                if disk['isShareable']:
                    shared_disks.append(disk['name'])
                    continue
            else:
                if disk['sharingType'] and not disk['sharingType'] == 'None':
                    shared_disks.append(disk['name'])
                    continue

            if not disk['isAttached']:
                disk.update({'metadata': {'attached_vm': None}})
                continue

            vm = self.getAttachedVms(disk)
            response = self.getVm(vm['href'])

            if response["status"] == 8:
                if not v2tAssessmentMode:
                    # Collect data for non-shared disks. As disks are not shared, only one attached VM is expected.
                    metadata = {'attached_vm': {
                        'href': vm['href'],
                        'name': vm['name'],
                    }}
                    self.createMetaDataInOrgVDC(disk['id'], metadata, entity='disk', domain='system')
                    disk.update({'metadata': metadata})
            else:
                disksWithoutPoweredOffVms.append(disk['name'])

        # Validation fails if shared disks exists
        if shared_disks:
            errors.append(f"Independent Disks in Org VDC are shared. Shared disks: {', '.join(shared_disks)}")
        else:
            logger.debug("Validated Successfully, Independent Disks in Source Org VDC are not shared")

        # Validation fails if VMs attached tp disks are not powered off
        if disksWithoutPoweredOffVms:
            errors.append(f"VMs attached to disks are not powered off: {', '.join(disksWithoutPoweredOffVms)}")
        else:
            logger.debug(
                "Validated Successfully, Independent Disks in Source Org VDC are attached to powered off VMs or not "
                "attached")

        if v2tAssessmentMode:
            return errors
        if errors:
            raise Exception('; '.join(errors))

    @isSessionExpired
    def getEdgeGatewayDhcpConfig(self, edgeGatewayId, v2tAssessmentMode=False):
        """
        Description :   Gets the DHCP Configuration details of the specified Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            logger.debug("Getting DHCP Services Configuration Details of Source Edge Gateway")
            # url to get dhcp config details of specified edge gateway
            errorList = list()
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_DHCP_CONFIG_BY_ID.format(edgeGatewayId))
            # relay url to get dhcp config details of specified edge gateway
            relayurl = "{}{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                         vcdConstants.NETWORK_EDGES,
                                         vcdConstants.EDGE_GATEWAY_DHCP_CONFIG_BY_ID.format(edgeGatewayId),
                                         vcdConstants.EDGE_GATEWAY_DHCP_RELAY_CONFIG_BY_ID)
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # call to get api to get dhcp config details of specified edge gateway
            response = self.restClientObj.get(url, headers)
            # call to get api to get dhcp relay config details of specified edge gateway
            relayresponse = self.restClientObj.get(relayurl, self.headers)
            if relayresponse.status_code == requests.codes.ok:
                relayresponsedict = xmltodict.parse(relayresponse.content)
                # checking if relay is configured in dhcp, if so raising exception
                if relayresponsedict['relay'] is not None:
                    errorList.append('DHCP Relay is configured in source edge gateway\n')
            else:
                errorList.append('Failed to retrieve DHCP Relay configuration of Source Edge Gateway with error code {} \n'.format(relayresponse.status_code))
                return errorList, None
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                if not v2tAssessmentMode and float(self.version) >= float(vcdConstants.API_VERSION_ZEUS) and self.nsxVersion.startswith('2.5.2') and responseDict['enabled']:
                    errorList.append("DHCP is enabled in source edge gateway but not supported in target\n")
                # checking if static binding is configured in dhcp, if so raising exception
                if responseDict.get('staticBindings', None):
                    errorList.append("Static binding is in DHCP configuration of Source Edge Gateway\n")
                logger.debug("DHCP configuration of Source Edge Gateway retrieved successfully")
                # returning the dhcp details
                return errorList, responseDict
            else:
                errorList.append('Failed to retrieve DHCP configuration of Source Edge Gateway with error code {} \n'.format(response.status_code))
                return errorList, None
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayFirewallConfig(self, edgeGatewayId, validation=True):
        """
        Description :   Gets the Firewall Configuration details of the specified Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            logger.debug("Getting Firewall Services Configuration Details of Source Edge Gateway")

            sourceOrgVDCId = self.rollback.apiData.get('sourceOrgVDC', {}).get('@id', str())
            orgVdcNetworks = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False,
                                                    sharedNetwork=True)

            errorList = list()
            # url to retrieve the firewall config details of edge gateway
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_FIREWALL_CONFIG_BY_ID.format(edgeGatewayId))
            # get api call to retrieve the firewall config details of edge gateway
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                # checking if firewall is enabled on edge gateway, if so returning the user defined firewall details, else raising exception
                if responseDict['firewall']['enabled'] != 'false':
                    logger.debug("Firewall configuration of Source Edge Gateway retrieved successfully")
                    userDefinedFirewall = [firewall for firewall in
                                           responseDict['firewall']['firewallRules']['firewallRule'] if
                                           firewall['ruleType'] == 'user']
                    if float(self.version) <= float(vcdConstants.API_VERSION_PRE_ZEUS):
                        # getting the default policy rules which the user has marked as 'DENY'
                        defaultFirewallRule = [defaultRule for defaultRule in responseDict['firewall']['firewallRules']['firewallRule'] if
                                               defaultRule['ruleType'] == 'default_policy' and defaultRule['action'] != 'accept']
                        userDefinedFirewall.extend(defaultFirewallRule)
                    if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                        # getting the default policy rules which the user has marked as 'DENY'
                        defaultFirewallRule = [defaultRule for defaultRule in
                                               responseDict['firewall']['firewallRules']['firewallRule'] if
                                               defaultRule['ruleType'] == 'default_policy' and defaultRule['action'] == 'accept']
                        userDefinedFirewall.extend(defaultFirewallRule)
                    if not validation:
                        return userDefinedFirewall
                    groupingobjects = []
                    for firewall in userDefinedFirewall:
                        if firewall.get('application'):
                            if firewall['application'].get('service'):
                                services = firewall['application']['service'] if isinstance(firewall['application']['service'], list) else [firewall['application']['service']]
                                for service in services:
                                    if service['protocol'] == "tcp" or service['protocol'] == "udp":
                                        if service['port'] == "any":
                                            errorList.append("Any as a TCP/UDP port present in the firewall rule '{}'\n".format(firewall['name']))
                        if firewall.get('source'):
                            if firewall['source'].get('vnicGroupId'):
                                errorList.append("vNicGroupId '{}' is present in the source of firewall rule '{}'\n".format(firewall['source']['vnicGroupId'], firewall['name']))
                            if firewall['source'].get('groupingObjectId'):
                                groupingobjects = firewall['source']['groupingObjectId'] if isinstance(firewall['source']['groupingObjectId'], list) else [firewall['source']['groupingObjectId']]
                                for groupingobject in groupingobjects:
                                    if 'network' in groupingobject:
                                        for network in orgVdcNetworks:
                                            if network['networkType'] == "DIRECT" and network['parentNetworkId']['id'] == groupingobject:
                                                errorList.append("Direct network '{}' cannot be used in the source of firewall rule : '{}'\n".format(network['name'], firewall['name']))
                                            elif network['id'] == groupingobject and network['networkType'] == 'ISOLATED':
                                                errorList.append("Isolated network '{}' cannot be used in the source of firewall rule : '{}'\n".format(network['name'], firewall['name']))
                                            elif network['id'] == groupingobject and network['networkType'] == 'NAT_ROUTED' and network.get('connection', {})['routerRef']['id'].split(':')[-1] != edgeGatewayId:
                                                errorList.append("Routed Network '{}' is connected to different edge gateway so it cannot be used in source of firewall rule : '{}'\n".format(network['name'], firewall['name']))
                                    if "ipset" not in groupingobject and "network" not in groupingobject:
                                        errorList.append("The grouping object type '{}' in the source of firewall rule '{}' is not supported\n".format(groupingobject, firewall['name']))
                        if firewall.get('destination'):
                            if firewall['destination'].get('vnicGroupId'):
                                errorList.append("vNicGroupId '{}' is present in the destination of firewall rule '{}'\n".format(firewall['destination']['vnicGroupId'], firewall['name']))
                            if firewall['destination'].get('groupingObjectId'):
                                groupingobjects = firewall['destination']['groupingObjectId'] if isinstance(firewall['destination']['groupingObjectId'], list) else [firewall['destination']['groupingObjectId']]
                                for groupingobject in groupingobjects:
                                    if 'network' in groupingobject:
                                        for network in orgVdcNetworks:
                                            if network['networkType'] == "DIRECT" and network['parentNetworkId']['id'] == groupingobject:
                                                errorList.append("Direct network '{}' cannot be used in the source of firewall rule : '{}'\n".format(network['name'], firewall['name']))
                                            elif network['id'] == groupingobject and network['networkType'] == 'ISOLATED':
                                                errorList.append("Isolated network '{}' cannot be used in the source of firewall rule : '{}'\n".format(network['name'], firewall['name']))
                                            elif network['id'] == groupingobject and network['networkType'] == 'NAT_ROUTED' and network.get('connection', {})['routerRef']['id'].split(':')[-1] != edgeGatewayId:
                                                errorList.append("Routed Network '{}' is connected to different edge gateway so it cannot be used in source of firewall rule : '{}'\n".format(network['name'], firewall['name']))
                                    if "ipset" not in groupingobject and "network" not in groupingobject:
                                        errorList.append("The grouping object type '{}' in the destination of firewall rule '{}' is not supported\n".format(groupingobject, firewall['name']))
                    return errorList
                else:
                    errorList.append('Firewall is disabled in source\n')
                    return errorList
            return [
                "Failed to retrieve the Firewall Configurations of Source Edge Gateway with error code {} \n".format(
                    response.status_code)]
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayNatConfig(self, edgeGatewayId, validation=True):
        """
        Description :   Gets the NAT Configuration details of the specified Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            errorList = list()
            logger.debug("Getting NAT Services Configuration Details of Source Edge Gateway")
            # url to retrieve the nat config details of the specified edge gateway
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_NAT_CONFIG_BY_ID.format(edgeGatewayId))
            # get api call to retrieve the nat config details of the specified edge gateway
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                if not validation:
                    return responseDict['nat']
                logger.debug("NAT configuration of Source Edge Gateway retrieved successfully")
                # checking if nat64 rules are present, if not raising exception
                if responseDict['nat']['nat64Rules']:
                    errorList.append('Nat64 rule is configured in source but not supported in Target\n')
                # checking if nat rules are present
                if responseDict['nat']['natRules']:
                    natrules = responseDict['nat']['natRules']['natRule']
                    natrules = natrules if isinstance(natrules, list) else [natrules]
                    # iterating over the nat rules
                    for natrule in natrules:
                        if natrule['action'] == "dnat" and "-" in natrule['translatedAddress'] or "/" in natrule['translatedAddress']:
                            errorList.append(
                                'Range of IPs or network found in this DNAT rule {} and range cannot be used in target edge gateway\n'.format(
                                    natrule['ruleId']))
                    return errorList, natrules
                else:
                    return errorList, False
            else:
                errorList.append(
                    'Failed to retrieve the NAT Configurations of Source Edge Gateway with error code {} \n'.format(
                        response.status_code))
                return errorList, False
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewaySSLVPNConfig(self, edgeGatewayId):
        """
        Description :   Gets the SSLVPN Configuration details on the Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            logger.debug("Getting SSLVPN Services Configuration Details of Source Edge Gateway")
            # url to retrieve sslvpn config info
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_SSLVPN_CONFIG.format(edgeGatewayId))
            # get api call to retrieve sslvpn config info
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                logger.debug("SSLVPN configuration of Source Edge Gateway retrieved successfully")
                # checking if sslvpn is enabled, if so raising exception
                if responseDict['sslvpnConfig']['enabled'] == "true":
                    return ['SSLVPN service is configured in the Source but not supported in the Target\n']
                else:
                    return []
            else:
                return ['Unable to get SSLVPN Services Configuration Details with error code {}\n'.format(response.status_code)]
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayL2VPNConfig(self, edgeGatewayId):
        """
        Description :   Gets the L2VPN Configuration details on the Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            logger.debug("Getting L2VPN Services Configuration Details of Source Edge Gateway")
            # url to retrieve the l2vpn config info
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_L2VPN_CONFIG.format(edgeGatewayId))
            # get api call to retrieve the l2vpn config info
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                logger.debug("L2VPN configuration of Source Edge Gateway retrieved Successfully")
                # checking if l2vpn is enabled, if so raising exception
                if responseDict['l2Vpn']['enabled'] == "true":
                    return ["L2VPN service is configured in the Source but not supported in the Target\n"]
                else:
                    return []
            else:
                return ['Unable to get L2VPN Services Configuration Details of Source Edge Gateway with error code {} \n'.format(response.status_code)]
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayLoadBalancerConfig(self, edgeGatewayId, ServiceEngineGroupName, nsxvObj, v2tAssessmentMode=False):
        """
        Description :   Gets the Load Balancer Configuration details on the Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
                        ServiceEngineGroupName - Name of service engine group for load balancer configuration (STRING)
                        nsxvObj - NSXVOperations class object (OBJECT)
                        v2tAssessmentMode - bool the sets whether v2tAssessmentMode is executing this method or not (BOOLEAN)
        """
        try:
            loadBalancerErrorList = []
            supportedLoadBalancerAlgo = ['round-robin', 'leastconn']
            supportedLoadBalancerPersistence = ['cookie', 'sourceip']
            logger.debug("Getting Load Balancer Services Configuration Details of Source Edge Gateway {}".format(edgeGatewayId))
            # url to retrieve the load balancer config info
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_LOADBALANCER_CONFIG.format(edgeGatewayId))
            # get api call to retrieve the load balancer config info
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                # checking if load balancer is enabled, if so raising exception
                if responseDict['loadBalancer']['enabled'] == "true":
                    if not v2tAssessmentMode and not float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                        return ["Load Balancer service is configured in the Source edge gateway but not supported in the Target\n"]

                    applicationRules = responseDict['loadBalancer'].get('applicationRule', [])

                    if applicationRules:
                        loadBalancerErrorList.append('Application rules are present in load balancer service but not supported in the Target\n')

                    # url for getting edge gateway load balancer virtual servers configuration
                    url = '{}{}'.format(
                        vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                        vcdConstants.EDGE_GATEWAY_VIRTUAL_SERVER_CONFIG.format(edgeGatewayId))
                    response = self.restClientObj.get(url, self.headers)
                    if response.status_code == requests.codes.ok:
                        virtualServersData = xmltodict.parse(response.content)
                        if virtualServersData['loadBalancer']:
                            virtualServersData = virtualServersData['loadBalancer']['virtualServer'] if isinstance(
                                virtualServersData['loadBalancer']['virtualServer'], list) else \
                                [virtualServersData['loadBalancer']['virtualServer']]
                        else:
                            virtualServersData = []
                    else:
                        return ['Failed to get source edge gateway load balancer virtual servers configuration with error code {} \n'.format(response.status_code)]

                    for virtualServer in virtualServersData:
                        if not virtualServer.get('defaultPoolId', None):
                            loadBalancerErrorList.append("Default pool is not configured in load balancer virtual server '{}'\n".format(virtualServer['name']))

                    # Fetching application profiles data from response
                    if responseDict['loadBalancer'].get('applicationProfile'):
                        applicationProfiles = responseDict['loadBalancer'].get('applicationProfile') \
                            if isinstance(responseDict['loadBalancer'].get('applicationProfile'), list) \
                            else [responseDict['loadBalancer'].get('applicationProfile')]
                    else:
                        applicationProfiles = []

                    for profile in applicationProfiles:
                        if profile.get('persistence') and profile['persistence']['method'] not in supportedLoadBalancerPersistence:
                            loadBalancerErrorList.append("Unsupported persistence type '{}' provided in application profile '{}'\n".format(profile['persistence']['method'], profile['name']))

                    # fetching load balancer pools data
                    if responseDict['loadBalancer'].get('pool', []):
                        lbPoolsData = responseDict['loadBalancer'].get('pool', [])
                        lbPoolsData = lbPoolsData if isinstance(lbPoolsData, list) else [lbPoolsData]
                        for pool in lbPoolsData:
                            if pool['algorithm'] not in supportedLoadBalancerAlgo:
                                loadBalancerErrorList.append("Unsupported algorithm '{}' provided in load balancer pool '{}'\n".format(pool['algorithm'], pool['name']))
                            if pool['transparent'] != 'false':
                                loadBalancerErrorList.append('{} pool has transparent mode enabled which is not supported\n'.format(pool['name']))
                    if not v2tAssessmentMode and not nsxvObj.ipAddress and not nsxvObj.username:
                        loadBalancerErrorList.append("NSX-V LoadBalancer service is enabled on Source Edge Gateway {}, but NSX-V details are not provided in user input file\n".format(edgeGatewayId))

                    if not v2tAssessmentMode:
                        serviceEngineGroupResultList = self.getServiceEngineGroupDetails()
                        if serviceEngineGroupResultList:
                            if not ServiceEngineGroupName:
                                loadBalancerErrorList.append("NSX-V LoadBalancer service is enabled on Source Edge Gateway {}, Service Engine Group must be present in userInput yaml\n".format(edgeGatewayId))
                            serviceEngineGroupDetails = [serviceEngineGroup for serviceEngineGroup in serviceEngineGroupResultList if serviceEngineGroup['name'] == ServiceEngineGroupName]

                            if not serviceEngineGroupDetails:
                                loadBalancerErrorList.append("Service Engine Group {} doesnot exist in Avi.\n".format(ServiceEngineGroupName))
                            else:
                                if serviceEngineGroupDetails[0].get('haMode') != 'LEGACY_ACTIVE_STANDBY':
                                    logger.warning("Service engine group has HA MODE '{}', if you keep using this you may incur some extra charges.".format(serviceEngineGroupDetails[0].get('haMode')))
                        else:
                           loadBalancerErrorList.append("Service Engine Group {} doesn't exist in Avi.\n".format(ServiceEngineGroupName))
            else:
                loadBalancerErrorList.append('Unable to get load balancer service configuration with error code {} \n'.format(response.status_code))
            return loadBalancerErrorList
        except Exception:
            raise


    @isSessionExpired
    def isStaticRouteAutoCreated(self, edgeGatewayID, nextHopeIp):
        """
                Description :   Gets the Static Routing Configuration details on the Edge Gateway
                                whether static routes are auto created by vcd for distributed routing or
                                user defined static routes.
                Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
                """
        try:
            # url to retrieve the routing config info
            url = "{}{}/{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES, edgeGatewayID, vcdConstants.VNIC)
            # get api call to retrieve the edge gateway config info
            response = self.restClientObj.get(url, self.headers)
            subnetMask = None
            primaryAddress = None
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                vNicsDetails = responseDict['vnics']['vnic']

                for vnicData in vNicsDetails:
                    if "portgroupName" in vnicData.keys() and "DLR_to_EDGE" in vnicData['portgroupName']:
                        primaryAddress = vnicData['addressGroups']['addressGroup']['primaryAddress']
                        subnetMask = vnicData['addressGroups']['addressGroup']['subnetMask']
                        break

            if subnetMask and ipaddress.ip_address(nextHopeIp) in ipaddress.ip_network('{}/{}'.format(primaryAddress, subnetMask), strict=False):
                logger.debug("Next hop IP {} belongs to network of {}.".format(nextHopeIp, ipaddress.ip_network('{}/{}'.format(primaryAddress, subnetMask), strict=False)))
                return True
            return False
        except:
            raise

    @isSessionExpired
    def getEdgeGatewayRoutingConfig(self, edgeGatewayId, validation=True, precheck=False):
        """
        Description :   Gets the Routing Configuration details on the Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            errorList = list()
            logger.debug("Getting Routing Configuration Details of Source Edge Gateway")
            # url to retrieve the routing config info
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_ROUTING_CONFIG.format(edgeGatewayId))
            # get api call to retrieve the routing config info
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                if not validation:
                    return responseDict['routing']
                # checking if static routes present in edgeGateways.
                # If Pre-Check then raise error, or else raise warning.
                try:
                    edgeGatewayName = ''
                    data = self.rollback.apiData[vcdConstants.SOURCE_EDGE_GW]
                    for item in data:
                        if item['id'].split(':')[-1] == edgeGatewayId:
                            edgeGatewayName = item['name']
                    if responseDict['routing']['staticRouting']['staticRoutes'] and not precheck:
                        if type(responseDict['routing']['staticRouting']['staticRoutes']['route']) is list:
                            for staticRoute in responseDict['routing']['staticRouting']['staticRoutes']['route']:
                                nextHopIp = staticRoute['nextHop']
                                if not self.isStaticRouteAutoCreated(edgeGatewayId, nextHopIp):
                                    logger.warning("Source OrgVDC EdgeGateway {} has static routes configured. Please "
                                                   "configure equivalent rules directly on external network Tier-0/VRF.\n".format(edgeGatewayName))
                                    break
                        elif type(responseDict['routing']['staticRouting']['staticRoutes']['route']) is dict or type(responseDict['routing']['staticRouting']['staticRoutes']['route']) is OrderedDict:
                            nextHopeIp = responseDict['routing']['staticRouting']['staticRoutes']['route']['nextHop']
                            if not self.isStaticRouteAutoCreated(edgeGatewayId, nextHopeIp):
                                logger.warning(("Source OrgVDC EdgeGateway {} has static routes configured. Please "
                                                "configure equivalent rules directly on external network Tier-0/VRF.\n".format(edgeGatewayName)))
                    elif responseDict['routing']['staticRouting']['staticRoutes'] and precheck:
                        if type(responseDict['routing']['staticRouting']['staticRoutes']['route']) is list:
                            for staticRoute in responseDict['routing']['staticRouting']['staticRoutes']['route']:
                                nextHopIp = staticRoute['nextHop']
                                if not self.isStaticRouteAutoCreated(edgeGatewayId, nextHopIp):
                                    errorList.append("WARNING : Source OrgVDC EdgeGateway {} has static routes configured. Please "
                                                     "configure equivalent rules directly on external network Tier-0/VRF.\n".format(edgeGatewayName))
                                    break
                        elif type(responseDict['routing']['staticRouting']['staticRoutes']['route']) is dict or type(responseDict['routing']['staticRouting']['staticRoutes']['route']) is OrderedDict:
                            nextHopeIp = responseDict['routing']['staticRouting']['staticRoutes']['route']['nextHop']
                            if not self.isStaticRouteAutoCreated(edgeGatewayId, nextHopeIp):
                                errorList.append(("WARNING : Source OrgVDC EdgeGateway {} has static routes configured."
                                                  " Please configure equivalent rules directly on external network Tier-0/VRF.\n".format(edgeGatewayName)))
                except KeyError:
                    logger.debug('Static routes not present in edgeGateway configuration.\n')
                # checking if routing is enabled, if so raising exception
                if responseDict['routing']['ospf']['enabled'] == "true":
                    errorList.append("OSPF routing protocol is configured in the Source but not supported in the "
                                     "Target\n")
                if errorList:
                    return errorList, None
                else:
                    logger.debug("Routing configuration of Source Edge Gateway retrieved Successfully")
                    return errorList, responseDict['routing']
            else:
                return ['Failed to get Routing service details with error code {} \n'.format(response.status_code)], None
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayIpsecConfig(self, edgeGatewayId):
        """
        Description :   Gets the IPSEC Configuration details on the Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            errorList = list()
            logger.debug("Getting IPSEC Services Configuration Details of Source Edge Gateway")
            # url to retrieve the ipsec config info
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_IPSEC_CONFIG.format(edgeGatewayId))
            headers = {'Authorization': self.headers['Authorization'], 'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
            # get api call to retrieve the ipsec config info
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                if responseDict['enabled']:
                    if responseDict['sites']:
                        sites = responseDict['sites']['sites']
                        sourceIPsecSite = sites if isinstance(sites, list) else [sites]
                        # iterating over source ipsec sites
                        for eachsourceIPsecSite in sourceIPsecSite:
                            # raising exception if ipsecSessionType is not equal to policybasedsession
                            if eachsourceIPsecSite['ipsecSessionType'] != "policybasedsession":
                                errorList.append(
                                    'Source IPSEC rule is having routebased session type which is not supported\n')
                            # raising exception if the ipsec encryption algorithm in the source ipsec rule  is not present in the target
                            if eachsourceIPsecSite['encryptionAlgorithm'] not in ["aes", "aes256", "aes-gcm"]:
                                errorList.append(
                                    'Source IPSEC rule is configured with unsupported encryption algorithm {}\n'.format(
                                        eachsourceIPsecSite['encryptionAlgorithm']))
                            # raising exception if the authentication mode is not psk
                            if eachsourceIPsecSite['authenticationMode'] != "psk":
                                errorList.append(
                                    'Authentication mode as Certificate is not supported in target edge gateway\n')
                            # raising exception if the digest algorithm is not supported in target
                            if eachsourceIPsecSite['digestAlgorithm'] != "sha1":
                                errorList.append(
                                    'The specified digest algorithm {} is not supported in target edge gateway\n'.format(
                                        eachsourceIPsecSite['digestAlgorithm']))
                        logger.debug("IPSEC configuration of Source Edge Gateway retrieved successfully")
                        return errorList, responseDict
                    else:
                        return errorList, responseDict
                else:
                    return errorList, responseDict
            else:
                errorList.append("Failed to retrieve the IPSEC Configurations of Source Edge Gateway with error code {} \n".format(response.status_code))
                return errorList, None
        except Exception:
            raise

    @isSessionExpired
    def getEdgegatewayBGPconfig(self, edgeGatewayId, validation=True, nsxtObj=None, v2tAssessmentMode=False):
        """
        Description :   Gets the BGP Configuration details on the Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
                        validation - True or False based on validation require
                        nsxtObj - object of nsxtOperations
        """
        try:
            errorList = list()
            # reading the data from metadata
            data = self.rollback.apiData
            logger.debug("Getting BGP Services Configuration Details of Source Edge Gateway")
            # url to retrieve the bgp config into
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_BGP_CONFIG.format(edgeGatewayId))
            # get api call to retrieve the bgp config info
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                if response.content:
                    responseDict = xmltodict.parse(response.content)
                    if not validation:
                        return responseDict['bgp']
                    # validate vrf lite  only if source bgp is enabled
                    if responseDict['bgp']['enabled'] != 'false':
                        if not v2tAssessmentMode and float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                            # get the target external network backed Tier-0 gateway
                            targetExternalBackingTypeValue = data['targetExternalNetwork']['networkBackings']['values'][0]['backingTypeValue']
                            # validate only if backing type is VRF
                            if targetExternalBackingTypeValue == 'NSXT_VRF_TIER0':
                                if self.nsxVersion.startswith('2.'):
                                    errorList.append('VRF is not supported in NSX-T version: {}'.format(self.nsxVersion))
                                tier0RouterName = data['targetExternalNetwork']['networkBackings']['values'][0]['parentTier0Ref']['id']
                                tier0Details = nsxtObj.getTier0GatewayDetails(tier0RouterName)
                                tier0localASnum = tier0Details['local_as_num']
                                if tier0Details['graceful_restart_config']['mode'] == 'DISABLE':
                                    tier0GracefulRestartMode = 'false'
                                else:
                                    tier0GracefulRestartMode = 'true'
                                if responseDict['bgp']['localASNumber'] != tier0localASnum:
                                    errorList.append('Source Edge gateway & Target Tier-0 Gateway - {} localAS number should be always same.'.format(tier0RouterName))
                                if responseDict['bgp']['gracefulRestart'] != tier0GracefulRestartMode:
                                    errorList.append('Source Edge gateway & Target Tier-0 Gateway - {} graceful restart mode should always be same and disabled.'.format(tier0RouterName))
                                if tier0GracefulRestartMode == 'true':
                                    errorList.append('Target Tier-0 Gateway - {} graceful restart mode should always be disabled.'.format(tier0RouterName))
                            logger.debug("BGP configuration of Source Edge Gateway retrieved successfully")
                            # returning bdp config details dict
                            return errorList, True
                return [], False
            else:
                return ["Failed to retrieve the BGP Configurations of Source Edge Gateway with error code {} \n".format(response.status_code)], False
        except Exception:
            raise

    @isSessionExpired
    def _checkTaskStatus(self, taskUrl, returnOutput=False, timeoutForTask=vcdConstants.VCD_CREATION_TIMEOUT):
        """
        Description : Checks status of a task in VDC
        Parameters  : taskUrl   - Url of the task monitored (STRING)
                      timeOutForTask - Timeout value to check the task status (INT)
        """
        if self.headers.get("Content-Type", None):
            del self.headers['Content-Type']
        timeout = 0.0
        # Get the task details
        output = ''
        try:
            while timeout < timeoutForTask:
                headers = {'Authorization': self.headers['Authorization'],
                           'Accept': vcdConstants.GENERAL_JSON_CONTENT_TYPE}
                response = self.restClientObj.get(url=taskUrl, headers=headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    logger.debug("Checking status for task : {}".format(responseDict["operationName"]))
                    if returnOutput:
                        output = responseDict['operation']
                        # rfind will search from right to left, here Id always comes in the last
                        output = output[output.rfind("(") + 1:output.rfind(")")]
                    if responseDict["status"] == "success":
                        logger.debug("Successfully completed task : {}".format(responseDict["operationName"]))
                        if not returnOutput:
                            return
                        return output
                    if responseDict["status"] == "error":
                        logger.error("Task {} is in Error state {}".format(responseDict["operationName"], responseDict['details']))
                        raise Exception(responseDict['details'])
                    msg = "Task {} is in running state".format(responseDict["operationName"])
                    logger.debug(msg)
                time.sleep(vcdConstants.VCD_CREATION_INTERVAL)
                timeout += vcdConstants.VCD_CREATION_INTERVAL
            raise Exception('Task {} could not complete in the allocate'
                            'd time.'.format(responseDict["operationName"]))
        except:
            raise

    def getOrgVDCComputePolicies(self):
        """
        Description :   Gets VDC Compute Policies
        """
        try:
            logger.debug("Getting Org VDC Compute Policies Details")
            # url to retrieve org vdc compute policies
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.VDC_COMPUTE_POLICIES)
            # get api call to retrieve org vdc compute policies
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved Org VDC Compute Policies details successfully")
                # returning the list of org vdc compute policies
                responseDict = response.json()
                # return responseDict['values']
                resultTotal = responseDict['resultTotal']
            pageNo = 1
            pageSizeCount = 0
            resultList = []
            logger.debug('Getting Org VDC Compute Policies')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.VDC_COMPUTE_POLICIES, pageNo,
                                                        vcdConstants.ORG_VDC_COMPUTE_POLICY_PAGE_SIZE)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('Org VDC Compute Policies result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
            logger.debug('Total Org VDC Compute Policies result count = {}'.format(len(resultList)))
            logger.debug('All Org VDC Compute Policies successfully retrieved')
            return resultList
        except Exception:
            raise

    def enableSourceOrgVdc(self, sourceOrgVdcId):
        """
        Description :   Re-Enables the Source Org VDC
        Parameters  :   sourceOrgVdcId  -   id of the source org vdc (STRING)
        """
        try:
            # Check if source org vdc was disabled
            if not self.rollback.metadata.get("preMigrationValidation", {}).get("orgVDCValidations"):
                return

            # reading data from metadata
            data = self.rollback.apiData
            # enabling the source org vdc only if it was previously enabled, else not
            if data['sourceOrgVDC']['IsEnabled'] == "true":
                logging.info("RollBack: Enabling Source Org-Vdc")
                sourceOrgVdcId = sourceOrgVdcId.split(':')[-1]
                # url to enable source org vdc
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.ENABLE_ORG_VDC.format(sourceOrgVdcId))
                # post api call to enable source org vdc
                response = self.restClientObj.post(url, self.headers)
                if response.status_code == requests.codes.no_content:
                    logger.debug("Source Org VDC Enabled successfully")
                else:
                    responseDict = xmltodict.parse(response.content)
                    raise Exception("Failed to Enable Source Org VDC: {}".format(responseDict['Error']['@message']))
            else:
                logger.debug("Not Enabling Source Org VDC since it was already disabled")
        except Exception:
            raise

    @isSessionExpired
    def _checkSuspendedVMsInVapp(self, vApp):
        """
        Description :   Send get request for vApp and check for suspended VM in response
        Parameters  :   vApp - data related to a vApp (DICT)
        """
        vAppResponse = self.restClientObj.get(vApp['@href'], self.headers)
        responseDict = xmltodict.parse(vAppResponse.content)
        # checking if the vapp has vms present in it
        if not responseDict['VApp'].get('Children'):
            logger.debug('Source vApp {} has no VM present in it.'.format(vApp['@name']))
            return
        # retrieving vms of the vapp
        vmList = responseDict['VApp']['Children']['Vm'] if isinstance(responseDict['VApp']['Children']['Vm'],
                                                                      list) else [
            responseDict['VApp']['Children']['Vm']]
        # iterating over the vms in the vapp
        for vm in vmList:
            if vm["@status"] == "3":
                self.suspendedVMList.append(vm['@name'])

    def validateSourceSuspendedVMsInVapp(self, sourceOrgVDCId):
        """
        Description :   Validates that there exists no VMs in suspended state in Source Org VDC
                        If found atleast single VM in suspended state then raises exception
        """
        try:
            self.suspendedVMList = list()
            sourceVappsList = self.getOrgVDCvAppsList(sourceOrgVDCId)
            if not sourceVappsList:
                return

            # iterating over the source vapps
            for vApp in sourceVappsList:
                self.thread.spawnThread(self._checkSuspendedVMsInVapp, vApp)
            self.thread.joinThreads()
            if self.suspendedVMList:
                raise Exception("VM: {} is in suspended state, Unable to migrate".format(','.join(self.suspendedVMList)))
            logger.debug("Validated Successfully, No Suspended VMs in Source Vapps")
        except Exception:
            raise

    @isSessionExpired
    def _checkVappWithOwnNetwork(self, vApp):
        """
        Description :   Send get request for vApp and check if vApp has its own vapp routed network in response
        Parameters  :   vApp - data related to a vApp (DICT)
        """
        # get api call to retrieve the vapp details
        response = self.restClientObj.get(vApp['@href'], self.headers)
        responseDict = xmltodict.parse(response.content)
        vAppData = responseDict['VApp']
        # checking if the networkConfig is present in vapp's NetworkConfigSection
        if vAppData['NetworkConfigSection'].get('NetworkConfig'):
            vAppNetworkList = vAppData['NetworkConfigSection']['NetworkConfig'] if isinstance(
                vAppData['NetworkConfigSection']['NetworkConfig'], list) else [
                vAppData['NetworkConfigSection']['NetworkConfig']]
            if vAppNetworkList:
                networkList = []
                DHCPEnabledNetworkList = []
                # iterating over the source vapp network list
                for vAppNetwork in vAppNetworkList:
                    if vAppNetwork['Configuration'].get('ParentNetwork'):
                        # if parent network is present, then name of parent network and name of the network itself should be same - means it is an org vdc network present in vapp, else raising exception since it's a vapp network
                        # Fence mode is always "bridged" for org vdc networks and "natRouted" for vApp routed network which is not supported and vapp network - when network name is not same as that of its parent network name.
                        if vAppNetwork['Configuration']['FenceMode'] == "natRouted" and vAppNetwork['@networkName'] != vAppNetwork['Configuration']['ParentNetwork']['@name']:
                            networkList.append(vAppNetwork['@networkName'])
                        else:
                            logger.debug("validation successful the vApp networks {} in vApp {} is not routed".format(vAppNetwork['@networkName'], vApp['@name']))
                    else:
                        # if parent network is absent then raising exception only if the  network gateway is not dhcp
                        if vAppNetwork['Configuration']['IpScopes']['IpScope']['Gateway'] != '196.254.254.254':
                            # the fence mode for isolated vApp network is isolated which is supported and for routed it is natRouted
                            if vAppNetwork['Configuration']['FenceMode'] != "isolated":
                                networkList.append(vAppNetwork['@networkName'])
                            else:
                                logger.debug("validation successful the vApp networks {} in vApp {} is not routed".format(vAppNetwork['@networkName'], vApp['@name']))
                            if vAppNetwork['Configuration'].get('Features', {}).get('DhcpService', {}).get('IsEnabled') == 'true':
                                if self.version >= vcdConstants.API_VERSION_ANDROMEDA:
                                    logger.debug(
                                        "validation successful the vApp networks {} in vApp {} is isolated with DHCP enabled".format(
                                            vAppNetwork['@networkName'], vApp['@name']))
                                else:
                                    logger.debug("validation failed the vApp networks {} in vApp {} is isolated with DHCP enabled".format(
                                                vAppNetwork['@networkName'], vApp['@name']))
                                DHCPEnabledNetworkList.append(vAppNetwork['@networkName'])
                        else:
                            logger.debug("Validated successfully {} network within vApp {} is not a Vapp Network".format(vAppNetwork['@networkName'], vApp['@name']))
                if networkList:
                    self.vAppNetworkDict[vApp['@name']] = networkList
                if DHCPEnabledNetworkList:
                    self.DHCPEnabled[vApp['@name']] = DHCPEnabledNetworkList

    def validateNoVappNetworksExist(self, sourceOrgVDCId):
        """
        Description :   Validates there exists no vapp routed network in source vapps
        """
        try:
            vAppNetworkList = list()
            self.vAppNetworkDict = dict()
            self.DHCPEnabled = dict()

            vAppList = self.getOrgVDCvAppsList(sourceOrgVDCId)
            if not vAppList:
                return

                # iterating over the source vapps
            for vApp in vAppList:
                # spawn thread for check vapp with own network task
                self.thread.spawnThread(self._checkVappWithOwnNetwork, vApp)
                # halt the main thread till all the threads complete execution
            self.thread.joinThreads()
            if self.vAppNetworkDict:
                for key, value in self.vAppNetworkDict.items():
                    vAppNetworkList.append('vAppName: ' + key + ' : NetworkName: ' + ', '.join(value))
                raise Exception("vApp Routed Network: '{}' exist in Source Org VDC".format(', '.join(vAppNetworkList)))
        except Exception:
            raise

    @isSessionExpired
    def _checkVappWithIsolatedNetwork(self, vApp, migration=False):
        """
        Description :   Send get request for vApp and check if vApp has its own vapp routed network in response
        Parameters  :   vApp - data related to a vApp (DICT)
        """
        # get api call to retrieve the vapp details
        response = self.restClientObj.get(vApp['@href'], self.headers)
        responseDict = xmltodict.parse(response.content)
        vAppData = responseDict['VApp']
        # checking if the networkConfig is present in vapp's NetworkConfigSection
        if vAppData['NetworkConfigSection'].get('NetworkConfig'):
            vAppNetworkList = vAppData['NetworkConfigSection']['NetworkConfig'] if isinstance(
                vAppData['NetworkConfigSection']['NetworkConfig'], list) else [
                vAppData['NetworkConfigSection']['NetworkConfig']]
            if vAppNetworkList:
                networkList = []
                DHCPEnabledNetworkList = []
                # iterating over the source vapp network list
                for vAppNetwork in vAppNetworkList:
                    if not vAppNetwork['Configuration'].get('ParentNetwork'):
                        # if parent network is absent then raising exception only if the  network gateway is not dhcp
                        if vAppNetwork['Configuration']['IpScopes']['IpScope']['Gateway'] != '196.254.254.254':
                            # Checking for dhcp configuration on vapp isolated networks
                            if vAppNetwork['Configuration'].get('Features', {}).get('DhcpService', {}).get('IsEnabled') == 'true':
                                if self.version >= vcdConstants.API_VERSION_ANDROMEDA:
                                    logger.debug("validation successful the vApp networks {} in vApp {} is isolated "
                                                 "with DHCP enabled".format(vAppNetwork['@networkName'], vApp['@name']))
                                else:
                                    logger.debug("validation failed the vApp networks {} in vApp {} is isolated with "
                                                 "DHCP enabled".format(vAppNetwork['@networkName'], vApp['@name']))
                                DHCPEnabledNetworkList.append(vAppNetwork['@networkName'])
                        else:
                            logger.debug("Validated successfully {} network within vApp {} is not a Vapp "
                                         "Network".format(vAppNetwork['@networkName'], vApp['@name']))
                if DHCPEnabledNetworkList and not migration:
                    self.DHCPEnabled[vApp['@name']] = DHCPEnabledNetworkList
                else:
                    return DHCPEnabledNetworkList

    def validateDHCPOnIsolatedvAppNetworks(self, sourceOrgVDCId, edgeGatewayDeploymentEdgeCluster=None, nsxtObj=None):
        """
        Description :   Validates there exists no vapp routed network in source vapps
        """
        try:
            vAppNetworkList = list()
            self.DHCPEnabled = dict()

            vAppList = self.getOrgVDCvAppsList(sourceOrgVDCId)
            if not vAppList:
                return

            # iterating over the source vapps
            for vApp in vAppList:
                # spawn thread for check vapp with own network task
                self.thread.spawnThread(self._checkVappWithIsolatedNetwork, vApp)
                # halt the main thread till all the threads complete execution
            self.thread.joinThreads()
            if self.DHCPEnabled:
                for key, value in self.DHCPEnabled.items():
                    vAppNetworkList.append('vAppName: ' + key + ' : NetworkName: ' + ', '.join(value))

                if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA):
                    edgeGatewayData = self.getOrgVDCEdgeGateway(sourceOrgVDCId)
                    if len(edgeGatewayData['values']) == 0:
                        if edgeGatewayDeploymentEdgeCluster is not None:
                            logger.debug(
                                "DHCP is enabled on Isolated vApp Network. But source edge gateway is not present.Checking edgeGatewayDeploymentEdgeCluster")
                            self.validateEdgeGatewayDeploymentEdgeCluster(edgeGatewayDeploymentEdgeCluster, nsxtObj)
                        else:
                            raise Exception("DHCP is enabled on Isolated vApp Network, but neither Source EdgeGateway is present nor 'EdgeGatewayDeploymentEdgeCluster' is provided in the input file.")
                    logger.debug("DHCP is enabled on vApp Isolated Network: '{}'".format(', '.join(vAppNetworkList)))
                else:
                    raise Exception("DHCP is enabled on vApp Isolated Network: '{}'".format(', '.join(vAppNetworkList)))
        except Exception:
            raise

    @isSessionExpired
    def _checkMediaAttachedToVM(self, vApp):
        """
            Description :   Send get request for vApp and check if VMs in vApp have media attached
            Parameters  :   vApp - data related to a vApp (DICT)
        """
        try:
            hrefVapp = vApp['@href']
            vmWithMediaList = list()
            # get api call to retrieve vapp details
            response = self.restClientObj.get(hrefVapp, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                # checking if vapp has vms in it
                if responseDict['VApp'].get('Children'):
                    vmList = responseDict['VApp']['Children']['Vm'] if isinstance(responseDict['VApp']['Children']['Vm'],
                                                                                  list) else [
                        responseDict['VApp']['Children']['Vm']]
                    # iterating over vms in the vapp
                    for vm in vmList:
                        mediaSettings = vm.get('VmSpecSection', {}).get('MediaSection', {}).get('MediaSettings', []) if isinstance(
                            vm.get('VmSpecSection', {}).get('MediaSection', {}).get('MediaSettings', []), list) else [
                            vm.get('VmSpecSection', {}).get('MediaSection', {}).get('MediaSettings', [])
                        ]
                        # iterating over the list of media settings of vm
                        for mediaSetting in mediaSettings:
                            # checking for the ISO media type that should be disconnected, else raising exception
                            if mediaSetting['MediaType'] == "ISO":
                                if mediaSetting['MediaState'] != "DISCONNECTED":
                                    vmWithMediaList.append(vApp['@name'] + ':' + vm['@name'])
                    if vmWithMediaList:
                        return vmWithMediaList
                    else:
                        logger.debug("Validated successfully that media of source vm {} is not connected".format(vm['@name']))
                else:
                    logger.debug("Source vApp {} has no VMs in it".format(vApp['@name']))
            else:
                raise Exception('Unable to get vApp details from vApp: {}'.format(vApp['@name']))
        except Exception:
            raise

    @isSessionExpired
    def getVCDVersion(self):
        """
           Description : Fetch vcd version from vCD cells information
        """
        url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VCD_CELLS)
        response = self.restClientObj.get(url, self.headers)
        responseDict = response.json()
        if response.status_code == requests.codes.ok:
            values = responseDict['values']
            if not values:
                raise Exception("No vCD cell present to fetch vCD version information")
            vCDVersion = values[0].get("productVersion", None)
            if not vCDVersion:
                raise Exception("Not able to fetch vCD version due to API response difference")
            else:
                return re.match("\d*\.\d*\.\d*", values[0].get("productVersion")).group()
        else:
            raise Exception(
                "Failed to fetch vCD version information - {}".format(responseDict['message']))

    @isSessionExpired
    def getVMsRelatedDataOfOrgVdc(self):
        """
           Description : Fetch all the VM related data of all NSX-V backed OrgVDC'S
        """

        data = {}
        # Query url to fetch the vm related data
        acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
        headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
        url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress), vcdConstants.ORG_VDC_QUERY)
        response = self.restClientObj.get(url, headers)
        if response.status_code == requests.codes.ok:
            responseDict = response.json()
            resultTotal = responseDict['total']
        else:
            # failure in retrieving the data of org vdc
            raise Exception(
                "Failed to fetch the org vdc's data")

        pageNo = 1
        pageSizeCount = 0
        resultList = []
        logger.debug('Getting org vdc details')
        while resultTotal > 0 and pageSizeCount < resultTotal:
            # Query url to fetch the vm related data
            url = "{}{}&page={}&pageSize={}&format=records".format(
                vcdConstants.XML_API_URL.format(self.ipAddress),
                vcdConstants.ORG_VDC_QUERY, pageNo,
                25)
            getSession(self)
            # get api call to retrieve the media details of organization with page number and page size count
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                listOfOrgVDC = responseDict["record"] if isinstance(
                    responseDict["record"], list) else [responseDict["record"]]
                resultList.extend(listOfOrgVDC)
                pageSizeCount += len(responseDict['record'])
                logger.debug('Org VDC result pageSize = {}'.format(pageSizeCount))
                pageNo += 1
                resultTotal = responseDict['total']
            else:
                # failure in retrieving the data of org vdc
                raise Exception(
                    "Failed to fetch the org vdc's data")
        logger.debug('Total Org VDC result count = {}'.format(len(resultList)))

        for orgVDC in resultList:
            if orgVDC["orgName"] not in data:
                data[orgVDC["orgName"]] = {}
            data[orgVDC["orgName"]][orgVDC["name"]] = {
                "numberOfVApps": orgVDC["numberOfVApps"],
                "numberOfVMs": orgVDC["numberOfVMs"],
                "memoryUsedMB": orgVDC["memoryUsedMB"],
                "numberOfRunningVMs": orgVDC["numberOfRunningVMs"]
            }
        return data

    @isSessionExpired
    def validateVappVMsMediaNotConnected(self, OrgVDCID, raiseError=False):
        """
        Description :   Validates none VM's media is connected from any of the Source vApps
        """
        try:
            orgvdcId = OrgVDCID.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(orgvdcId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
            else:
                raise Exception('Error occurred while retrieving Org VDC - {} details'.format(OrgVDCID))
            if not responseDict['AdminVdc']['ResourceEntities']:
                return
            sourceOrgVDCEntityList = responseDict['AdminVdc']['ResourceEntities']['ResourceEntity'] if isinstance(
                responseDict["AdminVdc"]['ResourceEntities']['ResourceEntity'], list) else [
                responseDict["AdminVdc"]['ResourceEntities']['ResourceEntity']]
            # creating source vapp list
            sourceVappList = [vAppEntity for vAppEntity in sourceOrgVDCEntityList if
                              vAppEntity['@type'] == vcdConstants.TYPE_VAPP]

            for sourceVapp in sourceVappList:
                # spawn thread for check media connected to vm in vapp
                self.thread.spawnThread(self._checkMediaAttachedToVM, sourceVapp, saveOutputKey=sourceVapp['@name'])
                # halt the main thread till all the threads complete execution
            self.thread.joinThreads()

            if self.thread.stop():
                raise Exception("Validation failed vApp/s exist VM/s with media connected")
            allVmWithMediaList = list()
            for each_vApp, eachVmValues in self.thread.returnValues.items():
                if eachVmValues is not None:
                    allVmWithMediaList.append(','.join(eachVmValues))
            if raiseError and allVmWithMediaList:
                raise Exception('The following VMs have media attached to it: {}'.format(', '.join(allVmWithMediaList)))
            elif raiseError == False and allVmWithMediaList:
                logger.warning('The following VMs have media attached to it: {}'.format(', '.join(allVmWithMediaList)))
            else:
                logger.debug("Validated successfully no vApp/s has VM/s with media connected")
        except Exception:
            raise

    def validateSourceNetworkPools(self):
        """
        Description :   Validates the source network pool is VXLAN backed
        """
        try:
            # reading data from metadata
            data = self.rollback.apiData
            # checking for the network pool associated with source org vdc
            if data['sourceOrgVDC'].get('NetworkPoolReference'):
                # source org vdc network pool reference dict
                networkPool = data['sourceOrgVDC']['NetworkPoolReference']
                # get api call to retrieve the info of source org vdc network pool
                networkPoolResponse = self.restClientObj.get(networkPool['@href'], self.headers)
                networkPoolDict = xmltodict.parse(networkPoolResponse.content)
                # checking if the source network pool is VXLAN backed
                if networkPoolDict['vmext:VMWNetworkPool']['@xsi:type'] == vcdConstants.VXLAN_NETWORK_POOL_TYPE or \
                        networkPoolDict['vmext:VMWNetworkPool']['@xsi:type'] == vcdConstants.VLAN_NETWORK_POOL_TYPE:
                    # success - source network pool is VXLAN backed
                    logger.debug("Validated successfully, source org VDC network pool {} is VXLAN backed".format(networkPoolDict['vmext:VMWNetworkPool']['@name']))
                else:
                    # fail - source network pool is not VXLAN backed
                    raise Exception("Source org VDC network pool {} is not VXLAN/VLAN backed".format(networkPoolDict['vmext:VMWNetworkPool']['@name']))
            else:
                raise Exception("No Network pool is associated with Source Org VDC")
        except Exception:
            raise

    def validateNoTargetOrgVDCExists(self, sourceOrgVDCName):
        """
        Description :   Validates the target Org VDC does not exist with same name as that of source Org VDC
                        with '-t' appended
                        Eg: source org vdc name :-  v-CokeOVDC
                            target org vdc name :-  v-CokeOVDC-t
        Parameters : sourceOrgVDCName - Name of the source Org VDC (STRING)
        """
        try:
            data = self.rollback.apiData
            # retrieving list instance of org vdcs under the specified organization in user input file
            orgVDCsList = data['Organization']['Vdcs']['Vdc'] if isinstance(data['Organization']['Vdcs']['Vdc'], list) else [data['Organization']['Vdcs']['Vdc']]
            # iterating over the list of org vdcs under the specified organization
            for orgVDC in orgVDCsList:
                # checking if target org vdc's name already exist in the given organization; if so raising exception
                if orgVDC['@name'] == "{}-v2t".format(sourceOrgVDCName):
                    raise Exception("Target Org VDC '{}-v2t' already exists".format(sourceOrgVDCName))
            logger.debug("Validated successfully, no target org VDC named '{}-v2t' exists".format(sourceOrgVDCName))
        except Exception:
            raise

    @isSessionExpired
    def validateEdgeGatewayDeploymentEdgeCluster(self, edgeClusterName=None, nsxObj=None):
        """
        Description :   Validates if edge transport nodes are present in edge cluster for edge gateway deployment
        Parameters  :   edgeClusterName     -   Name of the cluster (STRING)
                        nsxObj              -   Object of NSXTOperations class (OBJECT)
        """
        try:
            if edgeClusterName:
                edgeClusterData = nsxObj.fetchEdgeClusterDetails(edgeClusterName)
                edgeNodes = edgeClusterData['members'] if isinstance(edgeClusterData['members'], list) \
                    else [edgeClusterData['members']]
                if len(edgeNodes) < 1:
                    raise Exception(
                        "Edge Transport Nodes are not present in the cluster - {}, minimum 1 node should be present for edge gateway deployment"
                            .format(edgeClusterName))
                else:
                    logger.debug("Validated successfully Edge Transport Nodes are present in the cluster {}".format(
                            edgeClusterName))
        except:
            raise

    @isSessionExpired
    def getAllOrgVdc(self):
        """
        Description : Method that returns details of all org vdcs
        Returns     : List of all org vdcs (LIST)
        """
        data = list()
        # Query url to fetch the vm related data
        acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
        headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
        url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress), vcdConstants.ORG_VDC_QUERY)
        response = self.restClientObj.get(url, headers)
        if response.status_code == requests.codes.ok:
            responseDict = response.json()
            resultTotal = responseDict['total']
        else:
            # failure in retrieving the data of org vdc
            raise Exception(
                "Failed to fetch the org vdc's data")

        pageNo = 1
        pageSizeCount = 0
        resultList = []
        logger.debug('Getting org vdc details')
        while resultTotal > 0 and pageSizeCount < resultTotal:
            # Query url to fetch the vm related data
            url = "{}{}&page={}&pageSize={}&format=records".format(
                vcdConstants.XML_API_URL.format(self.ipAddress),
                vcdConstants.ORG_VDC_QUERY, pageNo,
                25)
            getSession(self)
            # get api call to retrieve the media details of organization with page number and page size count
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                listOfOrgVDC = responseDict["record"] if isinstance(
                    responseDict["record"], list) else [responseDict["record"]]
                resultList.extend(listOfOrgVDC)
                pageSizeCount += len(responseDict['record'])
                logger.debug('Org VDC result pageSize = {}'.format(pageSizeCount))
                pageNo += 1
                resultTotal = responseDict['total']
            else:
                # failure in retrieving the data of org vdc
                raise Exception(
                    "Failed to fetch the org vdc's data")
        logger.debug('Total Org VDC result count = {}'.format(len(resultList)))

        for orgVDC in resultList:
            data.append({
                        "name": orgVDC["name"],
                        "id": f"urn:vcloud:vdc:{orgVDC['href'].split('/')[-1]}",
                        "org": {"name": orgVDC["orgName"]}})
        return data

    @isSessionExpired
    def getBackingTypeOfOrgVDC(self, orgVDCId):
        """
        Description : Method that returns backing type of org vdc
        Parameters  : orgVDCId   -   ID of org vdc (STRING)
        Returns     : Backing type of org vdc - NSX_V/NSX_T (STRING)
        """
        url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ORG_VDC_CAPABILITIES.format(orgVDCId))
        response = self.restClientObj.get(url, self.headers)
        responseDict = response.json()
        if response.status_code == requests.codes.ok:
            values = responseDict['values']
            for value in values:
                # Checking backing type key in response values
                if value['name'] == 'vdcGroupNetworkProviderTypes':
                    return value['value'][0]
                if value['name'] == 'networkProvider':
                    return value['value']
            else:
                raise Exception("Unable to fetch backing type from capabilities of org vdc")
        else:
            # failure in retrieving the capabilities of org vdc
            raise Exception("Failed to fetch the capabilities of org vdc due to error - {}".format(responseDict['message']))

    @description("Checking Bridging Components")
    @remediate
    def checkBridgingComponents(self, orgVDCIDList, edgeClusterNameList, nsxtObj, vcenterObj):
        try:
            orgVdcNetworkList = list()
            for sourceOrgVDCId in orgVDCIDList:
                orgVdcNetworkList += self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)
            orgVdcNetworkList = list(filter(lambda network: network['networkType'] != 'DIRECT', orgVdcNetworkList))
            if orgVdcNetworkList:
                threading.current_thread().name = "BridgingChecks"
                logger.info("Checking for Bridging Components")
                logger.info('Validating NSX-T Bridge Uplink Profile does not exist')
                nsxtObj.validateBridgeUplinkProfile()

                logger.info('Validating Edge Cluster Exists in NSX-T and Edge Transport Nodes are not in use')
                nsxtObj.validateEdgeNodesNotInUse(edgeClusterNameList)

                nsxtObj.validateOrgVdcNetworksAndEdgeTransportNodes(edgeClusterNameList, orgVdcNetworkList)

                logger.info("Validating whether the edge transport nodes are accessible via ssh or not")
                nsxtObj.validateIfEdgeTransportNodesAreAccessibleViaSSH(edgeClusterNameList)

                logger.info("Validating whether the edge transport nodes are deployed on v-cluster or not")
                nsxtObj.validateEdgeNodesDeployedOnVCluster(edgeClusterNameList, vcenterObj)

                logger.info("Validating the max limit of bridge endpoint profiles in NSX-T")
                nsxtObj.validateLimitOfBridgeEndpointProfile(orgVdcNetworkList)
                logger.info("Successfully completed checks for Bridging Components")
        except:
            raise
        else:
            threading.current_thread().name = "MainThread"

    @description("Performing OrgVDC related validations")
    @remediate
    def orgVDCValidations(self, inputDict, vdcDict, sourceOrgVDCId, nsxtObj):
        """
        Description : Pre migration validation tasks for org vdc
        Parameters  : inputDict      -  dictionary of all the input yaml file key/values (DICT)
                      vdcDict        -  dictionary of the vcd details (DIC)
                      sourceOrgVDCId -  ID of source org vdc (STRING)
                      nsxtObj        -  Object of NSXT operations class holding all functions related to NSXT (OBJECT)
        """
        # Flag to check whether the org vdc was disabled or not
        disableOrgVDC = False
        try:
            logger.info(f'Starting with PreMigration validation tasks for org vdc "{vdcDict["OrgVDCName"]}"')

            logger.info('Validating NSX-T manager details')
            self.getNsxDetails(inputDict["NSXT"]["Common"]["ipAddress"])

            # validating whether target org vdc with same name as that of source org vdc exists
            logger.info("Validating whether target Org VDC already exists")
            self.validateNoTargetOrgVDCExists(vdcDict["OrgVDCName"])

            # validating org vdc fast provisioned
            #logger.info('Validating whether source Org VDC is fast provisioned')
            #self.validateOrgVDCFastProvisioned()

            # getting the target External Network details
            logger.info(
                'Getting the target External Network - {} details.'.format(vdcDict["ExternalNetwork"]))
            targetExternalNetwork = self.getExternalNetwork(vdcDict["ExternalNetwork"], validateVRF=True)
            if isinstance(targetExternalNetwork, Exception):
                raise targetExternalNetwork

            # getting the source dummy External Network details
            logger.info('Getting the source dummy External Network - {} details.'.format(
                inputDict["VCloudDirector"]["DummyExternalNetwork"]))
            dummyExternalNetwork = self.getExternalNetwork(inputDict["VCloudDirector"]["DummyExternalNetwork"],
                                                           isDummyNetwork=True)
            if isinstance(dummyExternalNetwork, Exception):
                raise dummyExternalNetwork

            # getting the source provider VDC details and checking if its NSX-V backed
            logger.info('Getting the source Provider VDC - {} details.'.format(vdcDict["NSXVProviderVDCName"]))
            sourceProviderVDCId, isNSXTbacked = self.getProviderVDCId(vdcDict["NSXVProviderVDCName"])
            self.getProviderVDCDetails(sourceProviderVDCId, isNSXTbacked)

            # validating the source network pool is VXLAN or VLAN backed
            logger.info("Validating Source Network Pool is VXLAN or VLAN backed")
            self.validateSourceNetworkPools()

            # validating whether source org vdc is NSX-V backed
            logger.info('Validating whether source Org VDC is NSX-V backed')
            self.validateOrgVDCNSXbacking(sourceOrgVDCId, sourceProviderVDCId, isNSXTbacked)

            #  getting the target provider VDC details and checking if its NSX-T backed
            logger.info(
                'Getting the target Provider VDC - {} details.'.format(vdcDict["NSXTProviderVDCName"]))
            targetProviderVDCId, isNSXTbacked = self.getProviderVDCId(vdcDict["NSXTProviderVDCName"])
            self.getProviderVDCDetails(targetProviderVDCId, isNSXTbacked)

            # validating hardware version of source and target Provider VDC
            logging.info('Validating Hardware version of Source Provider VDC: {} and Target Provider VDC: {}'.format(
                vdcDict["NSXVProviderVDCName"], vdcDict["NSXTProviderVDCName"]))
            self.validateHardwareVersion()

            # validating if the target provider vdc is enabled or not
            logger.info(
                'Validating Target Provider VDC {} is enabled'.format(vdcDict["NSXTProviderVDCName"]))
            self.validateTargetProviderVdc()

            # disable the source Org VDC so that operations cant be performed on it
            logger.info('Disabling the source Org VDC - {}'.format(vdcDict["OrgVDCName"]))
            disableOrgVDC = self.disableOrgVDC(sourceOrgVDCId)

            # validating the source org vdc placement policies exist in target PVDC also
            logger.info('Validating whether source org vdc - {} placement policies are present in target PVDC'.format(
                vdcDict["OrgVDCName"]))
            self.validateVMPlacementPolicy(sourceOrgVDCId)

            # validating whether source and target P-VDC have same vm storage profiles
            logger.info('Validating storage profiles in source Org VDC and target Provider VDC')
            self.validateStorageProfiles()

            # Getting Org VDC Edge Gateway Id
            sourceEdgeGatewayIdList = self.getOrgVDCEdgeGatewayId(sourceOrgVDCId)
            self.rollback.apiData['sourceEdgeGatewayId'] = sourceEdgeGatewayIdList

            logger.info("Validating Edge cluster for target edge gateway deployment")
            self.validateEdgeGatewayDeploymentEdgeCluster(vdcDict.get('EdgeGatewayDeploymentEdgeCluster', None), nsxtObj)

            # getting the source External Network details
            logger.info('Getting the source External Network details.')
            sourceExternalNetwork = self.getSourceExternalNetwork(sourceOrgVDCId)
            if isinstance(sourceExternalNetwork, Exception):
                raise sourceExternalNetwork

            # validating whether same subnet exist in source and target External networks
            logger.info('Validating source and target External networks have same subnets')
            self.validateExternalNetworkSubnets()

            logger.info('Validating if all edge gateways interfaces are in use')
            self.validateEdgeGatewayUplinks(sourceOrgVDCId, sourceEdgeGatewayIdList)

            # validating whether edge gateway have dedicated external network
            logger.info('Validating whether other Edge gateways are using dedicated external network')
            self.validateDedicatedExternalNetwork(inputDict, sourceEdgeGatewayIdList)

            # getting the source Org VDC networks
            logger.info('Getting the Org VDC networks of source Org VDC {}'.format(vdcDict["OrgVDCName"]))
            orgVdcNetworkList = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks')

            # validating DHCP service on Org VDC networks
            logger.info('Validating Isolated OrgVDCNetwork DHCP configuration')
            self.getOrgVDCNetworkDHCPConfig(orgVdcNetworkList)

            # validating whether DHCP is enabled on source Isolated Org VDC network
            edgeGatewayDeploymentEdgeCluster = vdcDict.get('EdgeGatewayDeploymentEdgeCluster', None)
            self.validateDHCPEnabledonIsolatedVdcNetworks(orgVdcNetworkList, sourceEdgeGatewayIdList, edgeGatewayDeploymentEdgeCluster,nsxtObj)

            # validating whether any org vdc network is shared or not
            logger.info('Validating whether shared networks are supported or not')
            self.validateOrgVDCNetworkShared(sourceOrgVDCId)

            # validating whether any source org vdc network is not direct network
            logger.info('Validating Source OrgVDC Direct networks')
            providerVDCImportedNeworkTransportZone = inputDict["VCloudDirector"].get("ImportedNeworkTransportZone", None)
            self.validateOrgVDCNetworkDirect(orgVdcNetworkList, vdcDict["NSXTProviderVDCName"],
                                             providerVDCImportedNeworkTransportZone, nsxtObj)

        except:
            # Enabling source Org VDC if premigration validation fails
            if disableOrgVDC:
                self.enableSourceOrgVdc(sourceOrgVDCId)
            raise
        else:
            return True

    @description("Performing services related validations")
    @remediate
    def servicesValidations(self, vdcDict, sourceOrgVDCId, nsxtObj, nsxvObj):
        """
        Description : Pre migration validation tasks related to services configured in org vdc
        Parameters  : vdcDict        -  dictionary of the vcd details (DIC)
                      sourceOrgVDCId -  ID of source org vdc (STRING)
                      nsxtObj        -  Object of NSXT operations class holding all functions related to NSXT (OBJECT)
                      nsxvObj        -  Object of NSXV operations class holding all functions related to NSXV (OBJECT)
        """
        try:
            # if NSXTProviderVDCNoSnatDestinationSubnet is passed to sampleInput else set it to None
            noSnatDestSubnet = vdcDict.get("NoSnatDestinationSubnet", None)

            # Fetching service engine group name from sampleInput
            ServiceEngineGroupName = vdcDict.get('ServiceEngineGroupName', None)
            # get distributed firewall configuration
            logger.info('Validating Distributed Firewall configuration')
            dfwConfigReturn = self.getDistributedFirewallConfig(sourceOrgVDCId, validation=True)
            if isinstance(dfwConfigReturn, Exception):
                raise dfwConfigReturn

            # get the list of services configured on source Edge Gateway
            ipsecConfigDict = self.getEdgeGatewayServices(nsxtObj, nsxvObj, noSnatDestSubnet,
                                                          ServiceEngineGroupName=ServiceEngineGroupName)

            # Writing ipsec config to api data dict for further use
            self.rollback.apiData['ipsecConfigDict'] = ipsecConfigDict
        except:
            raise
        else:
            return True

    @description("Performing vApp related validations")
    @remediate
    def vappValidations(self, vdcDict, sourceOrgVDCId, nsxtObj=None):
        """
        Description : Pre migration validation tasks related to vApps present in org vdc
        Parameters  : vdcDict        -  dictionary of the vcd details (DIC)
                      sourceOrgVDCId -  ID of source org vdc (STRING)
        """
        try:
            # validating whether there are empty vapps in source org vdc
            logger.info("Validating no empty vapps exist in source org VDC")
            self.validateNoEmptyVappsExistInSourceOrgVDC(sourceOrgVDCId)

            # validating the source org vdc does not have any suspended state vms in any of the vapps
            logger.info('Validating suspended state VMs does not exist in any of the Source vApps')
            self.validateSourceSuspendedVMsInVapp(sourceOrgVDCId)

            # Validating if fencing is enabled on vApps in source OrgVDC
            logger.info('Validating if fencing is enabled on vApps in source OrgVDC')
            self.validateVappFencingMode(sourceOrgVDCId)

            # validating that No vApps have its own vApp Networks
            logger.info('Validate vApps have no routed vApp Networks')
            self.validateNoVappNetworksExist(sourceOrgVDCId)

            # validating that No vApps have isolated networks with dhcp configured
            logger.info('Validating isolated vApp networks with DHCP enabled')
            self.validateDHCPOnIsolatedvAppNetworks(sourceOrgVDCId, vdcDict.get('EdgeGatewayDeploymentEdgeCluster', None), nsxtObj)

            logger.info("Validating Independent Disks")
            self.validateIndependentDisks(sourceOrgVDCId)

            logger.info('Validating whether media is attached to any vApp VMs')
            self.validateVappVMsMediaNotConnected(sourceOrgVDCId)

            # get the affinity rules of source Org VDC
            logger.info('Getting the VM affinity rules of source Org VDC {}'.format(vdcDict["OrgVDCName"]))
            self.getOrgVDCAffinityRules(sourceOrgVDCId)

            # disabling Affinity rules
            logger.info('Disabling source Org VDC affinity rules if its enabled')
            self.disableSourceAffinityRules()
        except:
            raise
        else:
            return True

    def preMigrationValidation(self, inputDict, vdcDict, sourceOrgVDCId, nsxtObj, nsxvObj, validateVapp=False, validateServices=False):
        """
        Description : Pre migration validation tasks
        Parameters  : inputDict      -  dictionary of all the input yaml file key/values (DICT)
                      vdcDict        -  dictionary of the vcd details (DIC)
                      sourceOrgVDCId -  ID of source org vdc (STRING)
                      nsxtObj        -  Object of NSXT operations class holding all functions related to NSXT (OBJECT)
                      nsxvObj        -  Object of NSXV operations class holding all functions related to NSXV (OBJECT)
                      validateVapp   -  Flag deciding whether to validate vApp or not (BOOLEAN)
                      validateServices- Flag deciding whether to validate edge gateway services or not (BOOLEAN)
        """
        try:
            # Replacing thread name with org vdc name
            threading.current_thread().name = self.vdcName

            self.getNsxDetails(inputDict["NSXT"]["Common"]["ipAddress"])

            if any([
                    # Performing org vdc related validations
                    self.orgVDCValidations(inputDict, vdcDict, sourceOrgVDCId, nsxtObj),
                    # Performing services related validations
                    self.servicesValidations(vdcDict, sourceOrgVDCId, nsxtObj, nsxvObj) if validateServices else False,
                    # Performing vApp related validations
                    self.vappValidations(vdcDict, sourceOrgVDCId, nsxtObj) if validateVapp else False]):
                logger.debug(
                    f'Successfully completed org vdc related validation tasks for org vdc "{vdcDict["OrgVDCName"]}"')
        except:
            logger.error(traceback.format_exc())
            raise

    @isSessionExpired
    def checkSameExternalNetworkUsedByOtherVDC(self,sourceOrgVDC, inputDict, externalNetworkName):
        """
                Description :   Validate if the External network is dedicatedly used by any other Org VDC edge gateway mentioned in the user specs file.
        """
        try:
            orgVdcList = inputDict['VCloudDirector']['SourceOrgVDC']
            orgVdcNameList= list()
            for orgVdc in orgVdcList:
                if orgVdc['OrgVDCName'] != sourceOrgVDC and orgVdc['ExternalNetwork'] == externalNetworkName:
                    orgUrl = self.getOrgUrl(inputDict["VCloudDirector"]["Organization"]["OrgName"])
                    sourceOrgVDCId = self.getOrgVDCDetails(orgUrl, orgVdc["OrgVDCName"], 'sourceOrgVDC')
                    sourceEdgeGatewayIdList = self.getOrgVDCEdgeGatewayId(sourceOrgVDCId)
                    if len(sourceEdgeGatewayIdList) > 0:
                        orgVdcNameList.append(orgVdc['OrgVDCName'])
            return orgVdcNameList
        except:
            raise

    @isSessionExpired
    def validateDedicatedExternalNetwork(self, inputDict, sourceEdgeGatewayIdList):
        """
        Description :   Validate if the External network is dedicatedly used by any other edge gateway
        """
        try:
            # reading the data from metadata
            data = self.rollback.apiData
            sourceOrgVDC = data['sourceOrgVDC']['@name']

            if 'targetExternalNetwork' not in data.keys():
                raise Exception('Target External Network not present')

            for sourceEdgeGatewayId in sourceEdgeGatewayIdList:
                sourceEdgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
                bgpConfigDict = self.getEdgegatewayBGPconfig(sourceEdgeGatewayId, validation=False)
                externalNetworkName = data['targetExternalNetwork']['name']
                if bgpConfigDict and isinstance(bgpConfigDict, dict) and bgpConfigDict['enabled'] == 'true':
                    orgVdcNameList= self.checkSameExternalNetworkUsedByOtherVDC(sourceOrgVDC, inputDict, externalNetworkName)
                    if len(orgVdcNameList) > 0:
                        raise Exception("BGP is not supported if multiple Org VDCs {} are using the same target external network {}.".format(orgVdcNameList, externalNetworkName))
                    if len(sourceEdgeGatewayIdList) > 1:
                        raise Exception('BGP is not supported in case of multiple edge gateways')
                    if data['targetExternalNetwork']['usedIpCount'] > 0:
                        raise Exception('Dedicated target external network is required as BGP is configured on source edge gateway')

            # Only validate dedicated ext-net if source edge gateways are present
            if sourceEdgeGatewayIdList:
                external_network_id = data['targetExternalNetwork']['id']
                url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                      vcdConstants.VALIDATE_DEDICATED_EXTERNAL_NETWORK_FILTER.format(external_network_id))
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    values = responseDict['values']
                    # checking whether values is a list if not converting it into a list
                    values = values if isinstance(values, list) else [values]
                    # iterating all the edge gateways
                    for value in values:
                        # checking whether the dedicated flag is enabled
                        if value['edgeGatewayUplinks'][0]['dedicated']:
                            raise Exception('Edge Gateway {} are using dedicated external network {} and hence new edge gateway cannot be created'.format(value['name'], data['targetExternalNetwork']['name']))
                    logger.debug('Validated Successfully, No other edge gateways are using dedicated external network')
                else:
                    raise Exception("Failed to retrieve edge gateway uplinks")
        except Exception:
            raise

    def deleteSession(self):
        """
        Description :   Deletes the current session / log out the current user
        """
        try:
            if self.vCDSessionId and self.VCD_SESSION_CREATED:
                logger.debug("Deleting the current user session (Log out current user)")
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.DELETE_CURRENT_SESSION.format(self.vCDSessionId))
                # delete api call to delete the current user session of vcloud director
                deleteResponse = self.restClientObj.delete(url, self.headers)
                if deleteResponse.status_code == requests.codes.no_content:
                    # successful log out of current vmware cloud director user
                    self.VCD_SESSION_CREATED = False
                    logger.debug("Successfully logged out VMware cloud director user")
                else:
                    # failure in current vmware cloud director user log out
                    deleteResponseDict = deleteResponse.json()
                    raise Exception("Failed to log out current user of VMware Cloud Director: {}".format(deleteResponseDict['message']))
        except Exception:
            raise

    @isSessionExpired
    def getEdgeGatewayDnsConfig(self, edgeGatewayId, validation=True):
        """
        Description :   Gets the DNS Configuration details of the specified Edge Gateway
        Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        try:
            # url to fetch edge gateway details
            getUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                   vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId))
            getResponse = self.restClientObj.get(getUrl, headers=self.headers)
            if getResponse.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(getResponse.content)
                edgeGatewayDict = responseDict['EdgeGateway']
                # checking if use default route for dns relay is enabled on edge gateway, if not then return
                if edgeGatewayDict['Configuration']['UseDefaultRouteForDnsRelay'] != 'true':
                    return []
            logger.debug("Getting DNS Services Configuration Details of Source Edge Gateway")
            # url to get DNS config details of specified edge gateway
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_DNS_CONFIG_BY_ID.format(edgeGatewayId))
            # call to get api to get dns config details of specified edge gateway
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                # checking if dns exists
                if responseDict['dns'].get('dnsViews'):
                    if responseDict['dns']['dnsViews']['dnsView']:
                        # returning the dns details
                        logger.debug("DNS configuration of Source Edge Gateway retrieved successfully")
                        if not validation:
                            return responseDict['dns']['dnsViews']['dnsView']['forwarders']
                        return []
                    if not validation:
                        return responseDict
                    return []
            else:
                return ["Failed to retrieve DNS configuration of Source Edge Gateway with error code {}".format(response.status_code)]
        except Exception:
            raise

    @isSessionExpired
    def _checkVappIsEmpty(self, vApp):
        """
        Description :   Send get request for vApp and check if vApp has VM or not in response
        Parameters  :   vApp - data related to a vApp (DICT)
        """
        try:
            vAppResponse = self.restClientObj.get(vApp['@href'], self.headers)
            if vAppResponse.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(vAppResponse.content)
                # checking if the vapp has vms present in it
                if 'VApp' in responseDict.keys():
                    if not responseDict['VApp'].get('Children'):
                        return True
                else:
                    raise Exception("Failed to get vApp details.")
        except Exception:
            raise

    def validateNoEmptyVappsExistInSourceOrgVDC(self, sourceOrgVDCId):
        """
        Description :   Validates that there are no empty vapps in source org vdc
                        If found atleast single empty vapp in source org vdc then raises exception
        """
        try:
            emptyvAppList = list()
            sourceVappsList = self.getOrgVDCvAppsList(sourceOrgVDCId)
            if not sourceVappsList:
                return

            # iterating over the source vapps
            for vApp in sourceVappsList:
                # spawn thread for check empty vApp task
                self.thread.spawnThread(self._checkVappIsEmpty, vApp, saveOutputKey=vApp['@name'])
            # halt the main thread till all the threads complete execution
            self.thread.joinThreads()
            if self.thread.stop():
                raise Exception("Validation failed, empty vapp/s exist in Source Org VDC")
            for vAppName, status in self.thread.returnValues.items():
                if status == True:
                    emptyvAppList.append(vAppName)
            if emptyvAppList:
                raise Exception('No VM exist in vApp: {}'.format(','.join(emptyvAppList)))
            else:
                logger.debug("Validated successfully, no empty vapps exist in Source Org VDC")
        except Exception:
            raise

    def validateHardwareVersion(self):
        """
        Description :   Validates Hardware version of Source Provider VDC and Target Provider VDC
        """
        try:
            # Reading api data from metadata
            data = self.rollback.apiData
            highestSourceVersion = 0
            highestSourceVersionName = str()
            highestTargetVersionName = str()
            for eachSourceVersionDetail in data['sourceProviderVDC']['Capabilities']['SupportedHardwareVersions']['SupportedHardwareVersion']:
                [name, currentVersion] = eachSourceVersionDetail['@name'].split('-')
                if int(currentVersion) > highestSourceVersion:
                    highestSourceVersion = int(currentVersion)
                highestSourceVersionName = '-'.join([name, str(highestSourceVersion)])
            highestTargetVersion = 0
            for eachTargetVersionDetail in data['targetProviderVDC']['Capabilities']['SupportedHardwareVersions']['SupportedHardwareVersion']:
                [name, currentVersion] = eachTargetVersionDetail['@name'].split('-')
                if int(currentVersion) > highestTargetVersion:
                    highestTargetVersion = int(currentVersion)
                highestTargetVersionName = '-'.join([name, str(highestTargetVersion)])
            if highestSourceVersion > highestTargetVersion:
                raise Exception(
                    'Hardware version on both Source Provider VDC and Target Provider VDC are not compatible, either both should be same or target PVDC hardware version'
                    ' should be greater than source PVDC hardware version. Source Provider VDC: {} and Target Provider VDC is: {}'.format(
                        highestSourceVersionName, highestTargetVersionName))
            else:
                logger.debug('Hardware version on both Source Provider VDC and Target Provider VDC are compatible')
        except Exception:
            raise

    def validateTargetOrgVDCState(self, targetOrgVDCId):
        """
        Description:    Validates that target Org VDC state is enabled or not
        Parameters:     targetOrgVDCId      - target Org VDC Id (STRING)
        """
        try:
            logger.debug('Getting target Org VDC details - {}'.format(targetOrgVDCId))
            # splitting the target org vdc id as per the requirements of xml api
            targetOrgVdcId = targetOrgVDCId.split(':')[-1]
            # url to retrieve the specified provider vdc details
            url = '{}{}'.format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                   vcdConstants.ORG_VDC_BY_ID.format(targetOrgVdcId))
            # get api call retrieve the specified provider vdc details
            response = self.restClientObj.get(url, self.headers)
            responseDict = xmltodict.parse(response.content)
            if response.status_code == requests.codes.ok:
                if responseDict['AdminVdc']['@id'] == targetOrgVDCId and responseDict['AdminVdc']['IsEnabled'] == "true":
                    logger.debug('Target Org VDC is enabled')
                    return
                else:
                    raise Exception("Target Org VDC is not enabled. Please enable it.")
        except Exception:
            raise

    @isSessionExpired
    def getCatalogMedia(self, orgId):
        """
        Description : Get all media objects of specific Organization
        Parameters  : orgId - Organization Id (STRING)
        """
        try:
            # url to get the media info of specified organization
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.GET_MEDIA_INFO)
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                       'X-VMWARE-VCLOUD-TENANT-CONTEXT': orgId}
            # get api call to retrieve the media details of organization
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['total']
            pageNo = 1
            pageSizeCount = 0
            resultList = []
            logger.debug('Getting media details')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                # url to get the media info of specified organization with page number and page size count
                url = "{}{}&page={}&pageSize={}&format=records".format(
                    vcdConstants.XML_API_URL.format(self.ipAddress),
                    vcdConstants.GET_MEDIA_INFO, pageNo,
                    vcdConstants.MEDIA_PAGE_SIZE)
                getSession(self)
                # get api call to retrieve the media details of organization with page number and page size count
                response = self.restClientObj.get(url, headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['record'])
                    pageSizeCount += len(responseDict['record'])
                    logger.debug('Media details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['total']
            logger.debug('Total media details result count = {}'.format(len(resultList)))
            logger.debug('Media details successfully retrieved')
            return resultList
        except Exception:
            raise

    @isSessionExpired
    def getvAppTemplates(self, orgId):
        """
        Description : Get all vApp Templates of specific Organization
        Parameters  : orgId - Organization Id (STRING)
        """
        try:
            # url to get vapp template info
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.GET_VAPP_TEMPLATE_INFO)
            acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                       'X-VMWARE-VCLOUD-TENANT-CONTEXT': orgId}
            # get api call to retrieve the vapp template details
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['total']
            pageNo = 1
            pageSizeCount = 0
            resultList = []
            logger.debug('Getting vapp template details')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                # url to get the vapp template info with page number and page size count
                url = "{}{}&page={}&pageSize={}&format=records".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                                                       vcdConstants.GET_VAPP_TEMPLATE_INFO, pageNo,
                                                                       vcdConstants.VAPP_TEMPLATE_PAGE_SIZE)
                getSession(self)
                # get api call to retrieve the vapp template details with page number and page size count
                response = self.restClientObj.get(url, headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['record'])
                    pageSizeCount += len(responseDict['record'])
                    logger.debug('vApp Template details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['total']
            logger.debug('Total vApp Template details result count = {}'.format(len(resultList)))
            logger.debug('vApp Template details successfully retrieved')
            return resultList
        except Exception:
            raise

    @isSessionExpired
    def disableSourceAffinityRules(self):
        """
        Description :   Disables the Affinity Rules in Source Vapp
        """
        try:
            data = self.rollback.apiData
            # checking if there exists affinity rules on source org vdc
            if data['sourceVMAffinityRules']:
                sourceAffinityRules = data['sourceVMAffinityRules'] if isinstance(data['sourceVMAffinityRules'], list) else [data['sourceVMAffinityRules']]
                # iterating over the affinity rules
                for sourceAffinityRule in sourceAffinityRules:
                    affinityID = sourceAffinityRule['@id']
                    # url to enable/disable the affinity rules
                    url = vcdConstants.ENABLE_DISABLE_AFFINITY_RULES.format(self.ipAddress, affinityID)
                    filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
                    vmReferencesPayloadData = ''
                    for eachVmReference in sourceAffinityRule['VmReferences']['VmReference']:
                        payloadDict = {'vmHref': eachVmReference['@href'],
                                       'vmId': eachVmReference['@id'],
                                       'vmName': eachVmReference['@name'],
                                       'vmType': eachVmReference['@type']}
                        payloadData = self.vcdUtils.createPayload(filePath,
                                                                  payloadDict,
                                                                  fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.VM_REFERENCES_TEMPLATE_NAME)
                        vmReferencesPayloadData += payloadData.strip("\"")
                    isEnabled = "false"
                    payloadDict = {'affinityRuleName': sourceAffinityRule['Name'],
                                   'isEnabled': isEnabled,
                                   'isMandatory': "true" if sourceAffinityRule['IsMandatory'] == "true" else "false",
                                   'polarity': sourceAffinityRule['Polarity'],
                                   'vmReferences': vmReferencesPayloadData}
                    payloadData = self.vcdUtils.createPayload(filePath,
                                                              payloadDict,
                                                              fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.ENABLE_DISABLE_AFFINITY_RULES_TEMPLATE_NAME)
                    payloadData = json.loads(payloadData)

                    self.headers['Content-Type'] = vcdConstants.GENERAL_XML_CONTENT_TYPE
                    # put api call to enable / disable affinity rules
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    responseDict = xmltodict.parse(response.content)
                    if response.status_code == requests.codes.accepted:
                        task_url = response.headers['Location']
                        # checking the status of the enabling/disabling affinity rules task
                        self._checkTaskStatus(taskUrl=task_url)
                        logger.debug('Affinity Rules got enabled successfully in Source')
                    else:
                        raise Exception('Failed to enable Affinity Rules in Source {} '.format(responseDict['Error']['@message']))
        except Exception:
            raise

    def enableSourceAffinityRules(self):
        """
        Description :   Enables Affinity Rules in Source VApp
        """
        try:
            # Check if source affinity rules were enabled or not
            if not self.rollback.metadata.get("preMigrationValidation", {}).get("vappValidations"):
                return

            logger.info("RollBack: Enable Source vApp Affinity Rules")
            data = self.rollback.apiData
            # checking if there exists affinity rules on source org vdc
            if data['sourceVMAffinityRules']:
                sourceAffinityRules = data['sourceVMAffinityRules'] if isinstance(data['sourceVMAffinityRules'], list) else [data['sourceVMAffinityRules']]
                # iterating over the affinity rules
                for sourceAffinityRule in sourceAffinityRules:
                    affinityID = sourceAffinityRule['@id']
                    # url to enable/disable the affinity rules
                    url = vcdConstants.ENABLE_DISABLE_AFFINITY_RULES.format(self.ipAddress, affinityID)
                    filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
                    vmReferencesPayloadData = ''
                    for eachVmReference in sourceAffinityRule['VmReferences']['VmReference']:
                        payloadDict = {'vmHref': eachVmReference['@href'],
                                       'vmId': eachVmReference['@id'],
                                       'vmName': eachVmReference['@name'],
                                       'vmType': eachVmReference['@type']}
                        payloadData = self.vcdUtils.createPayload(filePath,
                                                                  payloadDict,
                                                                  fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.VM_REFERENCES_TEMPLATE_NAME)
                        vmReferencesPayloadData += payloadData.strip("\"")
                    isEnabled = "true" if sourceAffinityRule['IsEnabled'] == "true" else "false"
                    payloadDict = {'affinityRuleName': sourceAffinityRule['Name'],
                                   'isEnabled': isEnabled,
                                   'isMandatory': "true" if sourceAffinityRule['IsMandatory'] == "true" else "false",
                                   'polarity': sourceAffinityRule['Polarity'],
                                   'vmReferences': vmReferencesPayloadData}
                    payloadData = self.vcdUtils.createPayload(filePath,
                                                              payloadDict,
                                                              fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.ENABLE_DISABLE_AFFINITY_RULES_TEMPLATE_NAME)
                    payloadData = json.loads(payloadData)

                    self.headers['Content-Type'] = vcdConstants.GENERAL_XML_CONTENT_TYPE
                    # put api call to enable / disable affinity rules
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    responseDict = xmltodict.parse(response.content)
                    if response.status_code == requests.codes.accepted:
                        task_url = response.headers['Location']
                        # checking the status of the enabling/disabling affinity rulres task
                        self._checkTaskStatus(taskUrl=task_url)
                        logger.debug('Affinity Rules got disabled successfully in Source')
                    else:
                        raise Exception('Failed to disable Affinity Rules in Source {} '.format(responseDict['Error']['@message']))
        except Exception:
            raise

    @staticmethod
    def ifIpBelongsToIpRange(ipAddr, startAddr, endAddr):
        """
            Description : Create an ip range
            Parameters :    ipAddr - IP address to be searched
                            startAddr - Start address ip (IP)
                            endAddr -  End address ip (IP)
        """
        startIp = startAddr.split('.')
        endIp = endAddr.split('.')
        ip = ipAddr.split('.')
        for i in range(4):
            if int(ip[i]) < int(startIp[i]) or int(ip[i]) > int(endIp[i]):
                return False
        return True

    @staticmethod
    def createIpRange(ipNetwork, startAddress, endAddress):
        """
        Description : Create an ip range
        Parameters : ipNetwork of the subnet that the start and end address belong to (STRING)
                     startAddress - Start address ip (IP)
                     endAddress -  End address ip (IP)
        """
        # Find the list of ip's belonging to the ip network/subnet
        listOfIPs = list(map(str, ipaddress.ip_network(ipNetwork, strict=False).hosts()))

        # Index of startAddress in the list
        firstIndex = listOfIPs.index(startAddress)
        # Index of endAddress in the list
        lastIndex = listOfIPs.index(endAddress)

        # Return IP range
        return listOfIPs[firstIndex:lastIndex+1]

    def getServiceEngineGroupDetails(self):
        """
        Description : Retrieve service engine group list from VCD
        Return      : List of service engine groups (LIST)
        """
        try:
            logger.debug("Getting Service Engine Group Details")
            # url to retrieve service engine group details
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.GET_SERVICE_ENGINE_GROUP_URI)
            # get api call to retrieve org vdc compute policies
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved Service Engine Group details successfully")
                # returning the list of org vdc compute policies
                responseDict = response.json()
                # return responseDict['values']
                resultTotal = responseDict['resultTotal']
            pageNo = 1
            pageSizeCount = 0
            resultList = []
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.GET_SERVICE_ENGINE_GROUP_URI, pageNo,
                                                        vcdConstants.SERVICE_ENGINE_GROUP_PAGE_SIZE)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('Service Engine Group result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']

            return resultList
        except Exception:
            raise

    @isSessionExpired
    def networkConflictValidation(self, sourceOrgVDCId):
        """
        Description: This Method check if there is a over lapping IPs with Routed and Isolated
        param: sourceOrgVDCId - Id of source oRg VDC
        return: idList - List of overlapping Isolated network's Ids
        """
        try:
            # getting all the source VDC networks
            orgvdcNetworkList = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False,
                                                       sharedNetwork=True)
            idList = list()
            if orgvdcNetworkList:
                # Iterating over network list
                for network in orgvdcNetworkList:
                    # We need to check conflict only for isolated networks
                    if network['networkType'] == 'ISOLATED':
                        # Creating ip_network from gateway cidr
                        isolatedNetworkAddress = ipaddress.ip_network(
                            f"{network['subnets']['values'][0]['gateway']}/"
                            f"{network['subnets']['values'][0]['prefixLength']}", strict=False)
                        # Iteraing over network list to check for conflicts
                        for networkToCheck in orgvdcNetworkList:
                            if network != networkToCheck:
                                # Creating ip_network from gateway cidr
                                networkToCheckAddress = ipaddress.ip_network(
                                    f"{networkToCheck['subnets']['values'][0]['gateway']}/"
                                    f"{networkToCheck['subnets']['values'][0]['prefixLength']}", strict=False)
                                # If the networks overlap it concludes a conflict
                                if isolatedNetworkAddress.overlaps(networkToCheckAddress):
                                    idList.append({'name': network['name'],
                                                   'id': network['id'],
                                                   'shared': network['shared']})
                self.rollback.apiData['ConflictNetworks'] = idList
                return idList
        except Exception:
            raise

    def validatingDFWobjects(self, orgVdcId, dfwRules, conflictIDs, allSecurityGroups):
        """
        Description: Method validates whether the network has conflicts
        parameter:  dfwRules:  All the DFW rules in source Org VDC
                    conflictIDs:  ID of the network used in dfw rules

        """
        try:
            # Fetching networks details from metadata dict
            orgVdcNetworks = self.getOrgVDCNetworks(orgVdcId, 'sourceOrgVDCNetworks', saveResponse=False, sharedNetwork=True)
            orgVdcNetworks = {network['name']: network for network in orgVdcNetworks}

            conflictNetworkNames = list()
            errorList = list()
            if not conflictIDs:
                logger.debug('No overlapping network present in the orgVDC')
            for rule in dfwRules:
                if rule.get('appliedToList'):
                    appliedToList = rule['appliedToList']['appliedTo'] \
                        if isinstance(rule['appliedToList']['appliedTo'], list) \
                        else [rule['appliedToList']['appliedTo']]
                else:
                    appliedToList = []

                appliedToNetworkEdges = set()
                for appliedToParam in appliedToList:
                    if appliedToParam['type'] not in vcdConstants.APPLIED_TO_LIST:
                        errorList.append(f'Unsupported type "{appliedToParam["type"]}" provided in applied to section in rule "{rule["name"]}"')
                    if appliedToParam['type'] == 'Network':
                        appliedToNet = orgVdcNetworks.get(appliedToParam['name'])
                        appliedToNetworkEdges.add(appliedToNet['connection']['routerRef']['id'])

                sources = destinations = list()
                if rule.get('sources'):
                    sources = rule['sources']['source'] if isinstance(
                        rule['sources']['source'], list) else [rule['sources']['source']]

                if rule.get('destinations'):
                    destinations = rule['destinations']['destination'] if isinstance(
                        rule['destinations']['destination'], list) else [rule['destinations']['destination']]

                # Collect network objects directly specified in rule and specified in Security Groups
                dfwRuleNetworks = set()
                for entity in sources+destinations:
                    if entity['type'] == 'Network':
                        dfwRuleNetworks.add((entity['name'], None))

                    if entity['type'] == 'SecurityGroup':
                        sourceGroup = allSecurityGroups[entity['value']]
                        includeMembers = sourceGroup.get('member', []) if isinstance(
                            sourceGroup.get('member', []), list) else [sourceGroup['member']]
                        dfwRuleNetworks.update([
                            (member['name'], sourceGroup['name'])
                            for member in includeMembers
                            if member['type']['typeName'] == 'Network'
                        ])

                sourceDFWNetworkDict = {}
                for dfwRuleNetwork, origin in dfwRuleNetworks:
                    orgVdcNetwork = orgVdcNetworks.get(dfwRuleNetwork)
                    if orgVdcNetwork['networkType'] == "DIRECT" and orgVdcNetwork['parentNetworkId']['name'] == dfwRuleNetwork:
                        errorList.append("Rule: {} has invalid objects: {}.".format(rule['name'], dfwRuleNetwork))
                    elif orgVdcNetwork['name'] == dfwRuleNetwork and orgVdcNetwork['networkType'] == 'NAT_ROUTED':
                        key = f"{dfwRuleNetwork}({origin})" if origin else dfwRuleNetwork
                        sourceDFWNetworkDict[key] = orgVdcNetwork['connection']['routerRef']['id']

                if len(set(sourceDFWNetworkDict.values())) > 1:
                    errorList.append(
                        f'Networks {list(sourceDFWNetworkDict.keys())} used in the source/destination of '
                        f'rule "{rule["name"]}" are connected to different edge gateways')

                dfwRuleNetworkEdges = set(sourceDFWNetworkDict.values())
                if appliedToNetworkEdges and dfwRuleNetworkEdges and not appliedToNetworkEdges == dfwRuleNetworkEdges:
                    errorList.append(
                        f'Networks used in the source/destination of rule "{rule["name"]}" and networks in '
                        f'"Applied to" sections are connected to different edge gateways')

            return errorList

        except Exception:
            raise

    @isSessionExpired
    def fetchAllExternalNetworks(self):
        try:
            # url to get all the external networks
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.ALL_EXTERNAL_NETWORKS)
            # get api call to get all the external networks
            response = self.restClientObj.get(url, self.headers)
            responseDict = response.json()
            if response.status_code == requests.codes.ok:
                logger.debug("External network details retrieved successfully.")
                resultTotal = responseDict['resultTotal']
                extNetData = copy.deepcopy(responseDict)
                extNetData['values'] = []
            else:
                raise Exception('Failed to retrieve External network details due to: {}'.format(responseDict['message']))
            pageNo = 1
            pageSizeCount = 0
            resultList = []
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.ALL_EXTERNAL_NETWORKS, pageNo,
                                                        15)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    extNetData['values'].extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('External network result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    responseDict = response.json()
                    raise Exception('Failed to get External network details due to: {}'.format(responseDict['message']))
            logger.debug('Total External network result count = {}'.format(len(resultList)))
            logger.debug('All External network successfully retrieved')
            return extNetData['values']
        except:
            raise

    @isSessionExpired
    def validateExternalNetworkdvpg(self, parentNetworkId, nsxtProviderVDCName, orgvdcNetwork, networkData):
        """
        Description: This method validates the external network used by direct networks

        """
        try:
            # url to retrieve the networks with external network id
            url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_ORG_VDC_NETWORKS, vcdConstants.QUERY_EXTERNAL_NETWORK.format(parentNetworkId['id']))
            # get api call to retrieve the networks with external network id
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                if int(responseDict['resultTotal']) > 1:
                    # Added validation for shared direct network
                    if networkData['shared']:
                        # Fetching all external networks from vCD
                        try:
                            externalNetworks = self.fetchAllExternalNetworks()
                        except Exception as err:
                            return None, str(err)

                        # Fetching external network used by direct network
                        for extNet in externalNetworks:
                            if extNet['name'] == parentNetworkId['name']:
                                extNetUsedByDirectNet = copy.deepcopy(extNet)
                                break
                        else:
                            return None, "External Network - '{}' used by direct network - '{}' is not present".format(parentNetworkId['name'], orgvdcNetwork)

                        for extNet in externalNetworks:
                            # Finding segment backed ext net for shared direct network
                            if parentNetworkId['name'] + '-v2t' == extNet['name']:
                                if [backing for backing in extNet['networkBackings']['values'] if
                                   backing['backingTypeValue'] == 'IMPORTED_T_LOGICAL_SWITCH']:
                                    # Fetching all subnets from source ext net used by direct network
                                    extNetUsedByDirectNetSubnets = [ipaddress.ip_network(
                                        f'{subnet["gateway"]}/{subnet["prefixLength"]}', strict=False)
                                                                    for subnet in extNetUsedByDirectNet['subnets']
                                                                    ['values']]
                                    # Fetching all subnets from nsxt segment backed external network
                                    nsxtSegmentBackedExtNetSubnets = [ipaddress.ip_network(
                                        f'{subnet["gateway"]}/{subnet["prefixLength"]}', strict=False)
                                                                      for subnet in extNet['subnets']['values']]
                                    # If all the subnets from source ext-net are not present in nsxt segment backed ext net, then raise exception
                                    if [gateway for gateway in extNetUsedByDirectNetSubnets if
                                       gateway not in nsxtSegmentBackedExtNetSubnets]:
                                        return None, f"All the External Network - '{parentNetworkId['name']}' subnets are not present in Target External Network - '{extNet['name']}'."
                                    break
                        else:
                            return None, f"NSXT segment backed external network {parentNetworkId['name']+'-v2t'} is not present, and it is required for this direct shared network - {orgvdcNetwork}\n"
                    else:
                        targetProviderVDCId, isNSXTbacked = self.getProviderVDCId(nsxtProviderVDCName)
                        # url to get all the external networks
                        url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EXTERNAL_NETWORKS, vcdConstants.SCOPE_EXTERNAL_NETWORK_QUERY.format(targetProviderVDCId))
                        response = self.restClientObj.get(url, self.headers)
                        if response.status_code == requests.codes.ok:
                            responseDict = response.json()
                            externalNetworkIds = [values['name'] for values in responseDict['values']]
                            if parentNetworkId['name'] not in externalNetworkIds:
                                return None, 'The external network - {} used in the network - {} must be scoped to Target provider VDC - {}\n'.format(parentNetworkId['name'], orgvdcNetwork, nsxtProviderVDCName)
                        else:
                            return None, 'Failed to get external network scoped to target PVDC - {} with error code - {}\n'.format(nsxtProviderVDCName, response.status_code)
                else:
                    try:
                        sourceExternalNetwork = self.fetchAllExternalNetworks()
                    except Exception as err:
                        return None, str(err)
                    externalList = [externalNetwork['networkBackings'] for externalNetwork in sourceExternalNetwork if
                                    externalNetwork['id'] == parentNetworkId['id']]

                    for value in externalList:
                        externalDict = value
                    for value in externalDict['values']:
                        if value['backingType'] != 'DV_PORTGROUP':
                            return None, 'The external network {} should be backed by VLAN if a dedicated direct network is connected to it'.format(parentNetworkId['name'])
                    backingid = [values['backingId'] for values in externalDict['values']]
                    url = '{}{}'.format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                        vcdConstants.GET_PORTGROUP_VLAN_ID.format(backingid[0]))
                    acceptHeader = vcdConstants.GENERAL_JSON_CONTENT_TYPE.format(self.version)
                    headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
                    # get api call to retrieve the networks with external network id
                    response = self.restClientObj.get(url, headers)
                    if response.status_code == requests.codes.ok:
                        responseDict = response.json()
                    return orgvdcNetwork, None
                return None, None
            else:
                raise Exception(' Failed to get Org VDC network connected to external network {} with error code - {} '.format(parentNetworkId['name'], response.status_code))
        except Exception:
            raise

    @isSessionExpired
    def checkSharedNetworksUsedByOrgVdc(self, inputDict, differentOwners=False):
        """"
            This method will take inputDict as a input and returns list sharednetworks from orgVdc Networks.
        """
        try:
            # Iterating over the list org vdc/s to fetch the org vdc id
            orgVDCIdList = list()
            for orgVDCDict in inputDict["VCloudDirector"]["SourceOrgVDC"]:
                orgUrl = self.getOrgUrl(inputDict["VCloudDirector"]["Organization"]["OrgName"])
                # Fetch org vdc id
                sourceOrgVDCId = self.getOrgVDCDetails(orgUrl, orgVDCDict["OrgVDCName"], 'sourceOrgVDC',
                                                       saveResponse=False)
                orgVDCIdList.append(sourceOrgVDCId)

            networkList = list()
            for orgVDCId in orgVDCIdList:
                networkList += self.getOrgVDCNetworks(orgVDCId, 'sourceOrgVDCNetworks', saveResponse=False, sharedNetwork=differentOwners)

            # check whether network is shared and create a list of all shared networks.
            orgVdcNetworkSharedList = list()
            for orgVdcNetwork in networkList:
                if bool(orgVdcNetwork['shared']):
                    orgVdcNetworkSharedList.append(orgVdcNetwork)

            # Filtering unique networks from the list
            orgVdcNetworkSharedList = {network['id']: network for network in orgVdcNetworkSharedList}

            if not orgVdcNetworkSharedList:
                logger.debug("Validated Successfully, No Source Org VDC Networks are shared")
            return list(orgVdcNetworkSharedList.values())
        except:
            raise

    @isSessionExpired
    def getVappUsingSharedNetwork(self, orgVdcNetworkSharedList):
        """
            This method will take list of shared networks and returns list of vApp which uses that shared network.
            Parameter : orgVdcNetworkSharedList - This contains list of shared network.
        """
        try:
            # get OrgVdc UUID
            uuid = self.rollback.apiData['Organization']['@id'].split(':')[-1]
            # get vApp network list which uses shared network using query API.
            vAppList = []
            resultList = []
            headers = {'X-VMWARE-VCLOUD-TENANT-CONTEXT': uuid,
                       'Accept': 'application/*+json;version={}'.format(self.version),
                       'Authorization': self.bearerToken}
            for orgVDCNetwork in orgVdcNetworkSharedList:
                networkName = orgVDCNetwork['name']
                queryUrl = vcdConstants.XML_API_URL.format(
                    self.ipAddress) + "query?type=vAppNetwork&filter=(linkNetworkName=={})".format(networkName)
                # response = self.restClientObj.get(queryUrl, headers=headers, auth=self.restClientObj.auth)
                response = self.restClientObj.get(queryUrl, headers=headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultTotal = responseDict['total']
                    pageNo = 1
                    pageSizeCount = 0
                    logger.debug('Getting vApp details')
                    while resultTotal > 0 and pageSizeCount < resultTotal:
                        # url to get the media info of specified organization with page number and page size count
                        url = "{}{}&page={}&pageSize={}&filter=(linkNetworkName=={})".format(
                            vcdConstants.XML_API_URL.format(self.ipAddress),
                            vcdConstants.VAPP_NETWORK_QUERY, pageNo,
                            vcdConstants.MEDIA_PAGE_SIZE, networkName)
                        getSession(self)
                        # get api call to retrieve the vApp network details of organization with page number and page size count
                        response = self.restClientObj.get(url, headers)
                        if response.status_code == requests.codes.ok:
                            responseDict = response.json()
                            resultList.extend(responseDict['record'])
                            pageSizeCount += len(responseDict['record'])
                            logger.debug('Media details result pageSize = {}'.format(pageSizeCount))
                            pageNo += 1
                            resultTotal = responseDict['total']
                    logger.debug('Total vApp network details result count = {}'.format(len(resultList)))
                    logger.debug('vApp network details successfully retrieved')
                for record in resultList:
                    vAppList.append(record['vAppName'])
            return vAppList
        except:
            raise

    @isSessionExpired
    def getOrgVdcOfvApp(self, vAppList):
        """
            This method takes vApplist as a input and return list of OrgVdc which belong to vApp.
            Parameter : vAppList - List of all vApp which is using shared network.
        """
        try:
            # get OrgVdc UUID
            uuid = self.rollback.apiData['Organization']['@id'].split(':')[-1]
            orgVdcvApplist = []
            orgVdcNameList = []
            resultList = []
            headers = {'X-VMWARE-VCLOUD-TENANT-CONTEXT': uuid,
                       'Accept': 'application/*+json;version={}'.format(self.version),
                       'Authorization': self.bearerToken}
            getvAppDataUrl = vcdConstants.VAPP_DATA_URL.format(self.ipAddress)
            response = self.restClientObj.get(getvAppDataUrl, headers=headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['total']
                pageNo = 1
                pageSizeCount = 0
                while resultTotal > 0 and pageSizeCount < resultTotal:
                    # url to get the vApp info of specified organization with page number and page size count
                    url = "{}{}&page={}&pageSize={}&format=records".format(
                        vcdConstants.XML_API_URL.format(self.ipAddress),
                        vcdConstants.VAPP_INFO_QUERY, pageNo,
                        vcdConstants.MEDIA_PAGE_SIZE)
                    getSession(self)
                    # get api call to retrieve the vApp details of organization with page number and page size count
                    response = self.restClientObj.get(url, headers)
                    if response.status_code == requests.codes.ok:
                        responseDict = response.json()
                        resultList.extend(responseDict['record'])
                        pageSizeCount += len(responseDict['record'])
                        logger.debug('Media details result pageSize = {}'.format(pageSizeCount))
                        pageNo += 1
                        resultTotal = responseDict['total']
                logger.debug('Total vApp details result count = {}'.format(len(resultList)))
            for record in resultList:
                tempDict = {}
                if record['name'] in vAppList:
                    tempDict['name'] = record['name']
                    tempDict['orgvdc'] = record['vdcName']
                    orgVdcNameList.append(record['vdcName'])
                    orgVdcvApplist.append(tempDict)
            return orgVdcvApplist, orgVdcNameList
        except:
            raise

    def checkMaxOrgVdcCount(self, sourceOrgVdcList, orgVdcNetworkSharedList):
        """
            This method raise an exception if number of OrgVdc are more than max count.
            Parameter : sourceOrgVdcList - List of all sourceOrgVdc.
        """
        try:
            if orgVdcNetworkSharedList:
                if len(sourceOrgVdcList) > vcdConstants.MAX_ORGVDC_COUNT:
                    raise Exception("In case of shared networks, the number of OrgVdcs to be parallely migrated should not be more than {}.".format(vcdConstants.MAX_ORGVDC_COUNT))
            else:
                logger.debug("No shared networks are present")
        except:
            raise

    def checkextraOrgVdcsOnSharedNetwork(self, orgVdcNameList, sourceOrgVdcList):
        """
            This method will check if any OrgVdc uses shared network other than OrgVdc mentioned in userSpecs.
            Parameter : OrgNameList - List contains name of all orgvdc's which is using shared network
                        sourceOrgVdcList - List contains name of Orgvdc's from input file, which is using shared network.
        """
        try:
            extraOrgVdcsOnSharedNetwork = [x for x in orgVdcNameList if x not in sourceOrgVdcList]
            if len(extraOrgVdcsOnSharedNetwork) > 0:
                raise Exception("OrgVdc/s : {}, also use shared network used by the OrgVdc/s {}. These also need to be added in input file".format(','.join(set(extraOrgVdcsOnSharedNetwork)), sourceOrgVdcList))
        except:
            raise

    def checkIfOwnerOfSharedNetworkAreBeingMigrated(self, inputDict):
        """
        Description: Check if owner of shared networks are also part of this migration or not
        """
        try:
            ownersOfSharedNetworks = list()
            orgVDCNameListToBeMigrated = [orgvdc['OrgVDCName'] for orgvdc in inputDict["VCloudDirector"]["SourceOrgVDC"]]
            networkOwnerMapping = dict()
            # get list shared network
            orgVdcNetworkSharedList = self.checkSharedNetworksUsedByOrgVdc(inputDict, differentOwners=True)
            for network in orgVdcNetworkSharedList:
                # get list of vApp which uses this shared network.
                vAppList = self.getVappUsingSharedNetwork([network])

                # get OrgVDC which belongs to vApp which uses shared network.
                _, orgVdcNameList = self.getOrgVdcOfvApp(vAppList)

                # Adding data to network owner mapping
                networkOwnerMapping[network['id']] = [network['ownerRef']['name'], orgVdcNameList]

            threading.current_thread().name = "MainThread"

            # If any vapp part of the org vdc's undergoing migration, then fetch the owner of shared networks
            for networkUsageData in networkOwnerMapping.values():
                if [orgvdc for orgvdc in networkUsageData[1] if orgvdc in orgVDCNameListToBeMigrated]:
                    ownersOfSharedNetworks.append(networkUsageData[0])
            ownersOfSharedNetworksNotPartOfMigration = [orgvdc for orgvdc in set(ownersOfSharedNetworks) if orgvdc not in orgVDCNameListToBeMigrated]

            if ownersOfSharedNetworksNotPartOfMigration:
                raise Exception(f"{', '.join(ownersOfSharedNetworksNotPartOfMigration)} are owners of shared networks, so they also need to added in input file for migration")
        except:
            raise
        finally:
            threading.current_thread().name = "MainThread"

    @staticmethod
    def validateDfwDefaultRuleForSharedNetwork(
            vcdObjList, sourceOrgVdcList, orgVdcNetworkSharedList, inputDict=None, orgVDCData=None):
        """
        Description :   Validates DFW default rule from all org VDCs is same if shared network is enabled.
        Parameters  :   vcdObjList - List of vcd operations class objects (LIST)
                        sourceOrgVdcList - List of all sourceOrgVdc (LIST)
                        orgVdcNetworkSharedList - List of shared networks from all org VDCs (LIST)
                        inputDict - All details from user input file (DICT)
                        orgVDCData - Details of Org VDCs (DICT)
        """
        if not orgVdcNetworkSharedList:
            return 

        dfwDefaultRules = []
        evaluatedOrgVdcs = []
        for vcdObj, orgVdcName in zip(vcdObjList, sourceOrgVdcList):
            if orgVDCData:
                sourceOrgVDCId = orgVDCData[orgVdcName]["id"]
            elif inputDict:
                orgUrl = vcdObj.getOrgUrl(inputDict["VCloudDirector"]["Organization"]["OrgName"])
                sourceOrgVDCId = vcdObj.getOrgVDCDetails(orgUrl, orgVdcName, 'sourceOrgVDC')
            else:
                raise Exception('Unable to find source Org VDC ID')

            try:
                defaultRule = vcdObj.getDistributedFirewallRules(sourceOrgVDCId, ruleType='default', validateRules=False)
                dfwDefaultRules.append(defaultRule)
                evaluatedOrgVdcs.append(orgVdcName)
            except DfwRulesAbsentError as e:
                logger.debug(f"{e} on {orgVdcName}")

        allValues = [
            (param, set(rule.get(param) for rule in dfwDefaultRules))
            for param in ['@disabled', 'action', 'direction', 'packetType', '@logged']
        ]
        conflictingKeys = [
            param
            for param, values in allValues
            if len(values) > 1
        ]
        if conflictingKeys:
            raise Exception(
                f"Distributed Firewall Default rule not common among Org VDCs: {', '.join(evaluatedOrgVdcs)}; "
                f"Conflicting parameters: {', '.join(conflictingKeys)}")

    @description("Performing checks for shared networks")
    @remediate
    def sharedNetworkChecks(self, inputDict, vcdObjList, orgVDCData):
        """
            This function will validate for the shared network scenario
            Parameter : InputDict- All details from user input file (DICT)
                        vcdObjList - List of vcd operations class objects (LIST)
                        orgVDCData - Details of Org VDCs (DICT)
        """
        try:
            # Shared networks are supported starting from Andromeda build
            if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA):
                # Get source OrgVdc names from input file.
                sourceOrgVdcData = inputDict["VCloudDirector"]["SourceOrgVDC"]
                sourceOrgVdcList = []
                for orgvdc in sourceOrgVdcData:
                    sourceOrgVdcList.append(orgvdc['OrgVDCName'])

                # get list shared network
                orgVdcNetworkSharedList = self.checkSharedNetworksUsedByOrgVdc(inputDict)

                # get list of vApp which uses shared network.
                vAppList = self.getVappUsingSharedNetwork(orgVdcNetworkSharedList)

                # get OrgVDC which belongs to vApp which uses shared network.
                orgVdcvApplist, orgVdcNameList = self.getOrgVdcOfvApp(vAppList)

                threading.current_thread().name = "MainThread"

                # Add validation
                logger.info("Performing checks for shared networks.")
                logger.info("Validating number of Org Vdc/s to be migrated are less/equal to max limit")
                self.checkMaxOrgVdcCount(sourceOrgVdcList, orgVdcNetworkSharedList)

                logger.info("Validating if any Org Vdc is using shared network other than those mentioned in input file")
                self.checkextraOrgVdcsOnSharedNetwork(orgVdcNameList, sourceOrgVdcList)

                logger.info("Validating if the owner of shared networks are also part of migration or not")
                self.checkIfOwnerOfSharedNetworkAreBeingMigrated(inputDict)

                logger.info("Validating distributed firewall default rule in all Org VDCs is same")
                self.validateDfwDefaultRuleForSharedNetwork(
                    vcdObjList, sourceOrgVdcList, orgVdcNetworkSharedList, orgVDCData=orgVDCData)
        except:
            raise
        else:
            logger.info(
                f'Successfully completed PreMigration validation tasks for org vdc/s'
                f' "{", ".join([vdc["OrgVDCName"] for vdc in inputDict["VCloudDirector"]["SourceOrgVDC"]])}"')
            return True
        finally:
            threading.current_thread().name = "MainThread"

    @isSessionExpired
    def getOrgVDCGroup(self):
        """
        Description: Fetch all DC groups present in vCD
        """
        try:
            # url to get Org vDC groups
            url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VDC_GROUPS)
            self.headers['Content-Type'] = 'application/json'
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
                pageNo = 1
                pageSizeCount = 0
                resultList = []
            else:
                errorDict = response.json()
                raise Exception("Failed to get target org VDC Group '{}' ".format(errorDict['message']))

            logger.debug('Getting data center group details')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                       vcdConstants.VDC_GROUPS, pageNo,
                                                                       25)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('DataCenter group details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    errorDict = response.json()
                    raise Exception("Failed to get target org VDC Group '{}' ".format(errorDict['message']))
            logger.debug('Total data center group details result count = {}'.format(len(resultList)))
            logger.debug('DataCenter group details successfully retrieved')
            return resultList
        except:
            raise

    @isSessionExpired
    def getSourceDfwSecurityGroups(self):
        url = "{}{}".format(
            vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
            'services/securitygroup/scope/{}'.format(self.rollback.apiData['sourceOrgVDC']['@id'].split(':')[-1])
        )
        self.headers['Content-Type'] = 'application/json'
        response = self.restClientObj.get(url, self.headers)
        securityGroups = []
        if response.status_code == requests.codes.ok:
            responseDict = xmltodict.parse(response.content)
            if responseDict.get('list'):
                securityGroups = (
                    responseDict['list']['securitygroup']
                    if isinstance(responseDict['list']['securitygroup'], list)
                    else [responseDict['list']['securitygroup']])

        return {group['objectId']: group for group in securityGroups}