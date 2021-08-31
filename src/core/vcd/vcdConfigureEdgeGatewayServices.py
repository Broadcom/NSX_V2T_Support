# ******************************************************
# Copyright © 2020-2021 VMware, Inc. All rights reserved.
# ******************************************************

"""
Description : Configuring Edge Gateway Services
"""

import logging
import json
import os
import random
import time
import ipaddress
import copy
import threading
import traceback

from collections import OrderedDict, defaultdict

import requests
import xmltodict

import src.core.vcd.vcdConstants as vcdConstants

from src.commonUtils.utils import Utilities
from src.core.vcd.vcdValidations import (
    VCDMigrationValidation, isSessionExpired, remediate, description, DfwRulesAbsentError, getSession)

logger = logging.getLogger('mainLogger')
chunksOfList = Utilities.chunksOfList

class ConfigureEdgeGatewayServices(VCDMigrationValidation):
    """
    Description : Class having edge gateway services configuration operations
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        vcdConstants.VCD_API_HEADER = vcdConstants.VCD_API_HEADER.format(self.version)
        vcdConstants.GENERAL_JSON_CONTENT_TYPE = vcdConstants.GENERAL_JSON_CONTENT_TYPE.format(self.version)
        vcdConstants.OPEN_API_CONTENT_TYPE = vcdConstants.OPEN_API_CONTENT_TYPE.format(self.version)

    def configureServices(self, nsxvObj, orgVDCDict):
        """
        Description :   Configure the  service to the Target Gateway
        Parameters  :   nsxvObj - NSXVOperations class object
                        orgVDCDict - Org VDC Input Dict (DICT)
        """
        try:
            # Setting thread name as vdc name
            threading.current_thread().name = self.vdcName

            noSnatDestSubnet = orgVDCDict.get('NoSnatDestinationSubnet')
            # Fetching load balancer vip configuration subnet from user input file
            loadBalancerVIPSubnet = orgVDCDict.get('LoadBalancerVIPSubnet')
            # Fetching service engine group name from sampleInput
            serviceEngineGroupName = orgVDCDict.get('ServiceEngineGroupName')

            metadata = self.rollback.metadata

            if not self.rollback.apiData['targetEdgeGateway']:
                logger.info('Skipping services configuration as edge gateway does '
                             'not exists')
                return

            targetEdgeGatewayIdList = [edgeGateway['id'] for edgeGateway in self.rollback.apiData['targetEdgeGateway']]
            ipsecConfigDict = self.rollback.apiData['ipsecConfigDict']

            # reading data from metadata
            data = self.rollback.apiData
            # taking target edge gateway id from apioutput json file
            targetOrgVdcId = data['targetOrgVDC']['@id']

            # Configuring target IPSEC
            self.configTargetIPSEC(ipsecConfigDict)
            # Configuring target NAT
            self.configureTargetNAT(noSnatDestSubnet)
            # Configuring firewall
            self.configureFirewall(networktype=False, configureIPSET=True)
            # Configuring BGP
            self.configBGP()
            # Configuring DNS
            self.configureDNS()
            # configuring loadbalancer
            self.configureLoadBalancer(nsxvObj, serviceEngineGroupName, loadBalancerVIPSubnet)
            logger.debug("Edge Gateway services configured successfully")
        except:
            logger.error(traceback.format_exc())
            raise

    @isSessionExpired
    def cidrCalculator(self, rangeofips):
        """
        Description : Convert the range od ips to CIDR format
        Parameters  : Range of ips (STRING)
        """
        try:
            # from parameter splitting the range of ip's with '-'
            start = rangeofips.split('-')[0].strip()
            end = rangeofips.split('-')[-1].strip()

            listOfIpsInIpRange = [str(ipaddress.IPv4Address(ip)) for ip in range(int(ipaddress.IPv4Address(start)), int(ipaddress.IPv4Address(end) + 1))]

            iplist = end.split('.')
            iplist.pop()
            iplist.append(str(0))
            ip = '.'.join(iplist)

            for CIDRPrefix in range(32, 0, -1):
                result = str(ip) + '/' + str(CIDRPrefix)
                ipsInNetworkFormed = [str(ip) for ip in ipaddress.ip_network(result, strict=False)]
                if all([True if ip in ipsInNetworkFormed else False for ip in
                        listOfIpsInIpRange]):
                    return str(result)

                if CIDRPrefix == 1:
                    return str(result)

        except Exception:
            raise

    @description("configuration of Firewall")
    @remediate
    def configureFirewall(self, networktype=False, configureIPSET=False):
        """
        Description :   Configure Firewall rules on target edge gateway
        Parameters  :   edgeGatewayId   -   id of the edge gateway (STRING)
                        targetOrgVDCId - ID of target org vdc (STRING)
                        networktype- False/true whether to configure security group or not
                                    default value will be false
        """
        try:
            firewallIdDict = list()
            for sourceEdgeGateway in self.rollback.apiData['sourceEdgeGateway']:
                logger.debug("Configuring Firewall Services in Target Edge Gateway - {}".format(sourceEdgeGateway['name']))
                sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
                edgeGatewayId = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'],
                                     self.rollback.apiData['targetEdgeGateway']))[0]['id']

                data = self.getEdgeGatewayFirewallConfig(sourceEdgeGatewayId, validation=False)
                # retrieving list instance of firewall rules from source edge gateway
                sourceFirewallRules = data if isinstance(data, list) else [data]
                # getting vcd id
                vcdid = self.rollback.apiData['sourceOrgVDC']['@id']
                vcdid = vcdid.split(':')[-1]
                # url to configure firewall rules on target edge gateway
                firewallUrl = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                              vcdConstants.ALL_EDGE_GATEWAYS,
                                              vcdConstants.T1_ROUTER_FIREWALL_CONFIG.format(edgeGatewayId))
                if not networktype:
                    # retrieving the application port profiles
                    applicationPortProfilesList = self.getApplicationPortProfiles()
                    url = "{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                        vcdConstants.GET_IPSET_GROUP_BY_ID.format(
                                            vcdConstants.IPSET_SCOPE_URL.format(vcdid)))
                    response = self.restClientObj.get(url, self.headers)
                    if response.status_code == requests.codes.ok:
                        responseDict = xmltodict.parse(response.content)
                        if responseDict.get('list'):
                            ipsetgroups = responseDict['list']['ipset'] if isinstance(responseDict['list']['ipset'],
                                                                                      list) else [
                                responseDict['list']['ipset']]
                        else:
                            ipsetgroups = []
                        if ipsetgroups:
                            if configureIPSET:
                                firewallIdDict = self.createIPSET(ipsetgroups, edgeGatewayId)
                                # creating a dict with firewallName as key and firewallIDs as value
                                # firewallIdDict = dict(zip(firewallName, firewallIDs))
                        firewallIdDict = self.rollback.apiData.get('firewallIdDict')
                # if firewall rules are configured on source edge gateway
                if sourceFirewallRules:
                    # firstTime variable is to check whether security groups are getting configured for the first time
                    firstTime = True
                    # iterating over the source edge gateway firewall rules
                    for firewallRule in sourceFirewallRules:
                        # if configStatus flag is already set means that the firewall rule is already configured, if so then skipping the configuring of same rule and moving to the next firewall rule
                        if self.rollback.apiData.get(firewallRule['id']) and not networktype:
                            if self.rollback.apiData[firewallRule['id']] == sourceEdgeGatewayId:
                                continue
                        data = dict()
                        ipAddressList = list()
                        applicationServicesList = list()
                        payloadDict = dict()
                        sourcefirewallGroupId = list()
                        destinationfirewallGroupId = list()
                        # checking for the source key in firewallRule dictionary
                        if firewallRule.get('source', None):
                            # retrieving ip address list source edge gateway firewall rule
                            if firewallRule['source'].get("ipAddress", None):
                                ipAddressList = firewallRule['source']['ipAddress'] if isinstance(
                                    firewallRule['source']['ipAddress'], list) else [
                                    firewallRule['source']['ipAddress']]
                            ipsetgroups = list()
                            networkgroups = list()
                            # retrieving ipset list source edge gateway firewall rule
                            if firewallRule['source'].get("groupingObjectId", None):
                                groups = firewallRule['source']['groupingObjectId'] if isinstance(
                                    firewallRule['source']['groupingObjectId'], list) else [
                                    firewallRule['source']['groupingObjectId']]
                                ipsetgroups = [group for group in groups if "ipset" in group]
                                networkgroups = [group for group in groups if "network" in group]
                            # checking if the networktype is false
                            if not networktype:
                                if ipAddressList:
                                    # creating payload data to create firewall group
                                    firewallGroupDict = {
                                        'name': firewallRule['name'] + '-' + 'Source-' + str(random.randint(1, 1000)),
                                        'edgeGatewayRef': {'id': edgeGatewayId},
                                        'ipAddresses': ipAddressList}
                                    firewallGroupDict = json.dumps(firewallGroupDict)
                                    # url to create firewall group
                                    firewallGroupUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                     vcdConstants.CREATE_FIREWALL_GROUP)
                                    self.headers['Content-Type'] = 'application/json'
                                    # post api call to create firewall group
                                    response = self.restClientObj.post(firewallGroupUrl, self.headers,
                                                                       data=firewallGroupDict)
                                    if response.status_code == requests.codes.accepted:
                                        # successful creation of firewall group
                                        taskUrl = response.headers['Location']
                                        firewallGroupId = self._checkTaskStatus(taskUrl=taskUrl, returnOutput=True)
                                        sourcefirewallGroupId.append(
                                            {'id': 'urn:vcloud:firewallGroup:{}'.format(firewallGroupId)})
                                    else:
                                        errorResponse = response.json()
                                        raise Exception(
                                            'Failed to create Firewall group - {}'.format(errorResponse['message']))
                                if ipsetgroups:
                                    # iterating all the IPSET in a firewall rule one by one
                                    for ipsetgroup in ipsetgroups:
                                        # url to retrieve the info of ipset group by id
                                        ipseturl = "{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                                                 vcdConstants.GET_IPSET_GROUP_BY_ID.format(ipsetgroup))
                                        # get api call to retrieve the ipset group info
                                        ipsetresponse = self.restClientObj.get(ipseturl, self.headers)
                                        if ipsetresponse.status_code == requests.codes.ok:
                                            # successful retrieval of ipset group info
                                            ipsetresponseDict = xmltodict.parse(ipsetresponse.content)
                                            # checking whether the key present in the IPSET firewallIdDict
                                            if firewallIdDict.get(edgeGatewayId):
                                                # checking wheather IPset name present in the dict
                                                if firewallIdDict[edgeGatewayId].get(ipsetresponseDict['ipset']['name']):
                                                    ipsetDict = firewallIdDict[edgeGatewayId][ipsetresponseDict['ipset']['name']]
                                                    sourcefirewallGroupId.append(ipsetDict)
                            # checking if any routed org vdc networks added in the firewall rule and networktype should be true
                            if networkgroups and networktype:
                                # checking if there are any network present in the fire wall rule
                                if len(networkgroups) != 0 and firstTime:
                                    logger.debug('Configuring security groups in the firewall in Target Edge Gateway - {}'.format(sourceEdgeGateway['name']))
                                    # Changing it into False because only want to log first time
                                    firstTime = False
                                # get api call to retrieve firewall info of target edge gateway
                                response = self.restClientObj.get(firewallUrl, self.headers)
                                if response.status_code == requests.codes.ok:
                                    userDefinedRulesList = list()
                                    # successful retrieval of firewall info
                                    responseDict = response.json()
                                    userDefinedRulesList = responseDict['userDefinedRules']
                                    for rule in userDefinedRulesList:
                                        name = rule['name'].split('-')[-1]
                                        if firewallRule['id'] == name:
                                            index = userDefinedRulesList.index(rule)
                                            userDefinedRulesList.pop(index)
                                            firewallGroupId = self.createSecurityGroup(networkgroups, firewallRule,
                                                                                       edgeGatewayId)
                                            if rule.get('sourceFirewallGroups'):
                                                for id in firewallGroupId:
                                                    rule['sourceFirewallGroups'].append({'id': '{}'.format(id)})
                                                data['userDefinedRules'] = userDefinedRulesList + [rule]
                                            else:
                                                for id in firewallGroupId:
                                                    sourcefirewallGroupId.append({'id': '{}'.format(id)})
                                                rule['sourceFirewallGroups'] = sourcefirewallGroupId
                                                data['userDefinedRules'] = userDefinedRulesList + [rule]
                                            payloadData = json.dumps(data)
                                            self.headers['Content-Type'] = 'application/json'
                                            # put api call to configure firewall rules on target edge gateway
                                            response = self.restClientObj.put(firewallUrl, self.headers,
                                                                              data=payloadData)
                                            if response.status_code == requests.codes.accepted:
                                                # successful configuration of firewall rules on target edge gateway
                                                taskUrl = response.headers['Location']
                                                self._checkTaskStatus(taskUrl=taskUrl)
                                                logger.debug(
                                                    'Firewall rule {} updated successfully with security group.'.format(
                                                        firewallRule['name']))
                                            else:
                                                # failure in configuration of firewall rules on target edge gateway
                                                response = response.json()
                                                raise Exception(
                                                    'Failed to update Firewall rule - {}'.format(response['message']))
                        ipAddressList = list()
                        # checking for the destination key in firewallRule dictionary
                        if firewallRule.get('destination', None):
                            # retrieving ip address list source edge gateway firewall rule
                            if firewallRule['destination'].get("ipAddress", None):
                                ipAddressList = firewallRule['destination']['ipAddress'] if isinstance(
                                    firewallRule['destination']['ipAddress'], list) else [
                                    firewallRule['destination']['ipAddress']]
                            ipsetgroups = list()
                            networkgroups = list()
                            # retrieving ipset group list source edge gateway firewall rule
                            if firewallRule['destination'].get("groupingObjectId", None):
                                groups = firewallRule['destination']['groupingObjectId'] if isinstance(
                                    firewallRule['destination']['groupingObjectId'], list) else [
                                    firewallRule['destination']['groupingObjectId']]
                                ipsetgroups = [group for group in groups if "ipset" in group]
                                networkgroups = [group for group in groups if "network" in group]
                            # checking if networktype is false
                            if not networktype:
                                if ipAddressList:
                                    # creating payload data to create firewall group
                                    firewallGroupDict = {'name': firewallRule['name'] + '-' + 'destination-' + str(
                                        random.randint(1, 1000)), 'edgeGatewayRef': {'id': edgeGatewayId},
                                                         'ipAddresses': ipAddressList}
                                    firewallGroupDict = json.dumps(firewallGroupDict)
                                    # url to create firewall group
                                    firewallGroupUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                     vcdConstants.CREATE_FIREWALL_GROUP)
                                    self.headers['Content-Type'] = 'application/json'
                                    # post api call to create firewall group
                                    response = self.restClientObj.post(firewallGroupUrl, self.headers,
                                                                       data=firewallGroupDict)
                                    if response.status_code == requests.codes.accepted:
                                        # successful creation of firewall group
                                        taskUrl = response.headers['Location']
                                        firewallGroupId = self._checkTaskStatus(taskUrl=taskUrl, returnOutput=True)
                                        destinationfirewallGroupId.append(
                                            {'id': 'urn:vcloud:firewallGroup:{}'.format(firewallGroupId)})
                                    else:
                                        errorResponse = response.json()
                                        raise Exception(
                                            'Failed to create Firewall group - {}'.format(errorResponse['message']))
                                if ipsetgroups:
                                    # iterating all the IPSET in a firewall rule one by one
                                    for ipsetgroup in ipsetgroups:
                                        # url to retrieve the info of ipset group by id
                                        ipseturl = "{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                                                 vcdConstants.GET_IPSET_GROUP_BY_ID.format(ipsetgroup))
                                        # get api call to retrieve the ipset group info
                                        ipsetresponse = self.restClientObj.get(ipseturl, self.headers)
                                        if ipsetresponse.status_code == requests.codes.ok:
                                            # successful retrieval of ipset group info
                                            ipsetresponseDict = xmltodict.parse(ipsetresponse.content)
                                            # checking whether the key present in the IPSET firewallIdDict
                                            if firewallIdDict.get(edgeGatewayId):
                                                # checking wheather IPset name present in the dict
                                                if firewallIdDict[edgeGatewayId].get(ipsetresponseDict['ipset']['name']):
                                                    ipsetDict = firewallIdDict[edgeGatewayId][ipsetresponseDict['ipset']['name']]
                                                    destinationfirewallGroupId.append(ipsetDict)
                            # checking if any routed org vdc networks added in the firewall rule and networktype should be true
                            if networkgroups and networktype:
                                # checking if there are any network present in the fire wall rule
                                if len(networkgroups) != 0 and firstTime:
                                    logger.info('Configuring security groups in the firewall')
                                    # Changing it into False because only want to log first time
                                    firstTime = False
                                # get api call to retrieve firewall info of target edge gateway
                                response = self.restClientObj.get(firewallUrl, self.headers)
                                if response.status_code == requests.codes.ok:
                                    userDefinedRulesList = list()
                                    # successful retrieval of firewall info
                                    responseDict = response.json()
                                    userDefinedRulesList = responseDict['userDefinedRules']
                                    for rule in userDefinedRulesList:
                                        name = rule['name'].split('-')[-1]
                                        if firewallRule['id'] == name:
                                            index = userDefinedRulesList.index(rule)
                                            userDefinedRulesList.pop(index)
                                            firewallGroupId = self.createSecurityGroup(networkgroups, firewallRule,
                                                                                       edgeGatewayId)
                                            if rule.get('destinationFirewallGroups'):
                                                for id in firewallGroupId:
                                                    rule['destinationFirewallGroups'].append({'id': '{}'.format(id)})
                                                data['userDefinedRules'] = userDefinedRulesList + [rule]
                                            else:
                                                for id in firewallGroupId:
                                                    destinationfirewallGroupId.append({'id': '{}'.format(id)})
                                                rule['destinationFirewallGroups'] = destinationfirewallGroupId
                                                data['userDefinedRules'] = userDefinedRulesList + [rule]
                                            payloadData = json.dumps(data)
                                            self.headers['Content-Type'] = 'application/json'
                                            # put api call to configure firewall rules on target edge gateway
                                            response = self.restClientObj.put(firewallUrl, self.headers,
                                                                              data=payloadData)
                                            if response.status_code == requests.codes.accepted:
                                                # successful configuration of firewall rules on target edge gateway
                                                taskUrl = response.headers['Location']
                                                self._checkTaskStatus(taskUrl=taskUrl)
                                                logger.debug(
                                                    'Firewall rule {} updated successfully with security group.'.format(
                                                        firewallRule['name']))
                                            else:
                                                # failure in configuration of firewall rules on target edge gateway
                                                response = response.json()
                                                raise Exception(
                                                    'Failed to update Firewall rule - {}'.format(response['message']))
                        if not networktype:
                            userDefinedRulesList = list()
                            # get api call to retrieve firewall info of target edge gateway
                            response = self.restClientObj.get(firewallUrl, self.headers)
                            if response.status_code == requests.codes.ok:
                                # successful retrieval of firewall info
                                responseDict = response.json()
                                userDefinedRulesList = responseDict['userDefinedRules']
                            # updating the payload with source firewall groups, destination firewall groups, user defined firewall rules, application port profiles
                            action = 'ALLOW' if firewallRule['action'] == 'accept' else 'DROP'
                            payloadDict.update({'name': firewallRule['name'] + "-" + firewallRule['id'],
                                                'enabled': firewallRule['enabled'], 'action': action})
                            payloadDict['sourceFirewallGroups'] = sourcefirewallGroupId if firewallRule.get('source',
                                                                                                            None) else []
                            payloadDict['destinationFirewallGroups'] = destinationfirewallGroupId if firewallRule.get(
                                'destination', None) else []
                            payloadDict['logging'] = "true" if firewallRule['loggingEnabled'] == "true" else "false"
                            # checking for the application key in firewallRule
                            if firewallRule.get('application'):
                                if firewallRule['application'].get('service'):
                                    # list instance of application services
                                    firewallRules = firewallRule['application']['service'] if isinstance(
                                        firewallRule['application']['service'], list) else [
                                        firewallRule['application']['service']]
                                    # iterating over the application services
                                    for applicationService in firewallRules:
                                        # if protocol is not icmp
                                        if applicationService['protocol'] != "icmp":
                                            protocol_name, port_id = self._searchApplicationPortProfile(
                                                applicationPortProfilesList,
                                                applicationService['protocol'],
                                                applicationService['port'])
                                            applicationServicesList.append({'name': protocol_name, 'id': port_id})
                                            payloadDict['applicationPortProfiles'] = applicationServicesList
                                        else:
                                            # if protocol is icmp
                                            # iterating over the application port profiles
                                            for value in applicationPortProfilesList:
                                                if value['name'] == vcdConstants.ICMP_ALL:
                                                    protocol_name, port_id = value['name'], value['id']
                                                    applicationServicesList.append(
                                                        {'name': protocol_name, 'id': port_id})
                                                    payloadDict["applicationPortProfiles"] = applicationServicesList
                            else:
                                payloadDict['applicationPortProfiles'] = applicationServicesList
                            data['userDefinedRules'] = userDefinedRulesList + [
                                payloadDict] if userDefinedRulesList else [payloadDict]
                            payloadData = json.dumps(data)
                            self.headers['Content-Type'] = 'application/json'
                            # put api call to configure firewall rules on target edge gateway
                            response = self.restClientObj.put(firewallUrl, self.headers, data=payloadData)
                            if response.status_code == requests.codes.accepted:
                                # successful configuration of firewall rules on target edge gateway
                                taskUrl = response.headers['Location']
                                self._checkTaskStatus(taskUrl=taskUrl)
                                # setting the configStatus flag meaning the particular firewall rule is configured successfully in order to skip its reconfiguration
                                self.rollback.apiData[firewallRule['id']] = sourceEdgeGatewayId
                                logger.debug('Firewall rule {} created successfully.'.format(firewallRule['name']))
                            else:
                                # failure in configuration of firewall rules on target edge gateway
                                response = response.json()
                                raise Exception('Failed to create Firewall rule on target Edge gateway {} - {}'.format(sourceEdgeGateway['name'], response['message']))
                    if not networktype:
                        logger.debug(f"Firewall rules configured successfully on target Edge gateway {sourceEdgeGateway['name']}")
                    if not firstTime:
                        logger.debug(f"Successfully configured security groups for Edge gateway {sourceEdgeGateway['name']}")
        except Exception:
            # Saving metadata in org VDC
            self.saveMetadataInOrgVdc()
            raise

    @description("configuration of Target IPSEC")
    @remediate
    def configTargetIPSEC(self, ipsecConfig):
        """
        Description :   Configure the IPSEC service to the Target Gateway
        Parameters  :   ipsecConfig   -   Details of IPSEC configuration  (DICT)
        """
        try:
            logger.info('Configuring Target Edge gateway services.')
            logger.debug('IPSEC is getting configured')

            # Acquiring lock due to vCD multiple org vdc transaction issue
            self.lock.acquire(blocking=True)

            targetEdgeGateway = copy.deepcopy(self.rollback.apiData['targetEdgeGateway'])
            targetEdgegatewayIdList = [(edgeGateway['id'], edgeGateway['name']) for edgeGateway in targetEdgeGateway]
            data = self.rollback.apiData
            IPsecStatus = data.get('IPsecStatus', {})
            for t1gatewayId, targetEdgeGatewayName in targetEdgegatewayIdList:
                # Status dict for ipsec config
                ipsecConfigured = IPsecStatus.get(t1gatewayId, [])
                ipsecConfigDict = ipsecConfig.get(targetEdgeGatewayName)
                # checking if ipsec is enabled on source org vdc edge gateway, if not then returning
                if not ipsecConfigDict or not ipsecConfigDict['enabled']:
                    logger.debug('IPSec is not enabled or configured in source Org VDC for edge gateway - {}.'.format(
                        targetEdgeGatewayName
                    ))
                    continue
                logger.debug("Configuring IPSEC Services in Target Edge Gateway - {}".format(targetEdgeGatewayName))
                # if enabled then retrieving the list instance of source  ipsec
                sourceIPsecSite = ipsecConfigDict['sites']['sites'] if isinstance(ipsecConfigDict['sites']['sites'], list) else [ipsecConfigDict['sites']['sites']]
                # url to configure the ipsec rules on target edge gateway
                url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                      vcdConstants.T1_ROUTER_IPSEC_CONFIG.format(t1gatewayId))
                # if configured ipsec rules on source org vdc edge gateway, then configuring the same on target edge gateway
                if ipsecConfigDict['enabled']:
                    for sourceIPsecSite in sourceIPsecSite:
                        # if configStatus flag is already set means that the sourceIPsecSite rule is already configured, if so then skipping the configuring of same rule and moving to the next sourceIPsecSite rule
                        if sourceIPsecSite['name'] in ipsecConfigured:
                            continue
                        # if the subnet is not a list converting it in the list
                        externalIpCIDR = sourceIPsecSite['localSubnets']['subnets'] if isinstance(sourceIPsecSite['localSubnets']['subnets'], list) else [sourceIPsecSite['localSubnets']['subnets']]
                        RemoteIpCIDR = sourceIPsecSite['peerSubnets']['subnets'] if isinstance(sourceIPsecSite['peerSubnets']['subnets'], list) else [sourceIPsecSite['peerSubnets']['subnets']]
                        # creating payload dictionary
                        payloadDict = {"name": sourceIPsecSite['name'],
                                       "enabled": "true" if sourceIPsecSite['enabled'] else "false",
                                       "localId": sourceIPsecSite['localId'],
                                       "externalIp": sourceIPsecSite['localIp'],
                                       "peerIp": sourceIPsecSite['peerId'],
                                       "RemoteIp": sourceIPsecSite['peerIp'],
                                       "psk": sourceIPsecSite['psk'],
                                       "connectorInitiationMode": " ",
                                       "securityType": "DEFAULT",
                                       "logging": "true" if ipsecConfigDict['logging']['enable'] else "false"
                                       }
                        filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
                        # creating payload data
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.CREATE_IPSEC_TEMPLATE)
                        payloadData = json.loads(payloadData)
                        # adding external ip cidr to payload
                        payloadData['localEndpoint']['localNetworks'] = externalIpCIDR
                        # adding remote ip cidr to payload
                        payloadData['remoteEndpoint']['remoteNetworks'] = RemoteIpCIDR
                        payloadData = json.dumps(payloadData)
                        self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                        # post api call to configure ipsec rules on target edge gateway
                        response = self.restClientObj.post(url, self.headers, data=payloadData)
                        if response.status_code == requests.codes.accepted:
                            # if successful configuration of ipsec rules
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl=taskUrl)
                            # adding a key here to make sure the rule have configured successfully and when remediation skipping this rule
                            #self.rollback.apiData[sourceIPsecSite['name']] = True
                            ipsecConfigured.append(sourceIPsecSite['name'])
                            IPsecStatus.update({t1gatewayId: ipsecConfigured})
                            data['IPsecStatus'] = IPsecStatus
                            logger.debug('IPSEC is configured successfully on the Target Edge Gateway - {}'.format(targetEdgeGatewayName))
                        else:
                            # if failure configuration of ipsec rules
                            response = response.json()
                            raise Exception('Failed to configure configure IPSEC on Target Edge Gateway {} - {} '
                                            .format(targetEdgeGatewayName, response['message']))
                    # below function configures network property of ipsec rules
                    self.connectionPropertiesConfig(t1gatewayId, ipsecConfigDict)
                else:
                    # if no ipsec rules are configured on source edge gateway
                    logger.debug('No IPSEC rules configured in source edge gateway - {}'.format(targetEdgeGatewayName))
        except Exception:
            raise
        finally:
            # Releasing thread lock
            try:
                self.lock.release()
            except RuntimeError:
                pass

    @isSessionExpired
    def getApplicationPortProfiles(self):
        """
        Description :   Get Application Port Profiles
        """
        try:
            # fetching name of NSX-T backed provider vdc
            tpvdcName = self.rollback.apiData['targetProviderVDC']['@name']

            # fetching NSX-T manager id
            nsxtManagerId = self.getNsxtManagerId(tpvdcName)

            url = "{}{}?filter=_context=={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.APPLICATION_PORT_PROFILES,
                                                    nsxtManagerId)
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
            else:
                response = response.json()
                raise Exception('Failed to fetch application port profile {} '.format(response['message']))
            pageNo = 1
            pageSizeCount = 0
            resultList = list()
            logger.debug('Getting Application port profiles')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}&filter=_context=={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.APPLICATION_PORT_PROFILES, pageNo,
                                                        vcdConstants.APPLICATION_PORT_PROFILES_PAGE_SIZE,
                                                                            nsxtManagerId)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    resultTotal = responseDict['resultTotal']
                    logger.debug('Application Port Profiles result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                else:
                    response = response.json()
                    raise Exception('Failed to fetch application port profile {} '.format(response['message']))
            logger.debug('Total Application Port Profiles result count = {}'.format(len(resultList)))
            logger.debug('Application Port Profiles successfully retrieved')
            return resultList
        except Exception:
            raise

    @isSessionExpired
    def _searchApplicationPortProfile(self, applicationPortProfilesList, protocol, port):
        """
        Description :   Search for specific Application Port Profile
        Parameters  :   applicationPortProfilesList - application port profiles list (LIST)
                        protocol - protocal for the Application Port profile (STRING)
                        port - Port for the application Port profile (STRING)
        """
        try:
            # fileName = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'apiOutput.json')
            # data = self.vcdUtils.readJsonData(fileName)
            data = self.rollback.apiData
            protocol = protocol.upper()
            for value in applicationPortProfilesList:
                if len(value['applicationPorts']) == 1:
                    if value['scope'] == 'SYSTEM':
                        if value['applicationPorts'][0]['protocol'] == protocol and value['applicationPorts'][0]['destinationPorts'][0] == port:
                            logger.debug('Application Port Profile for the specific protocol'
                                         ' and port retrieved successfully')
                            return value['name'], value['id']
                    elif value['scope'] == 'TENANT' and value.get('orgRef'):
                        if value['applicationPorts'][0]['protocol'] == protocol and value['applicationPorts'][0]['destinationPorts'][0] == port:
                            logger.debug('Application Port Profile for the specific protocol'
                                         ' and port retrieved successfully')
                            return value['name'], value['id']
            else:
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.APPLICATION_PORT_PROFILES)
                payloadDict = {
                    "name": "CUSTOM-" + protocol + "-" + port,
                    "applicationPorts": [{
                        "protocol": protocol,
                        "destinationPorts": [port]
                    }],
                    "orgRef": {
                        "name": data['Organization']['@name'],
                        "id": data['Organization']['@id']
                    },
                    "contextEntityId": data['targetOrgVDC']['@id'],
                    "scope": "TENANT"
                }
                payloadData = json.dumps(payloadDict)
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                response = self.restClientObj.post(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    portprofileID = self._checkTaskStatus(taskUrl=taskUrl, returnOutput=True)
                    logger.debug('Application port profile is created successfully ')
                    customID = 'urn:vcloud:applicationPortProfile:' + portprofileID
                    return payloadDict['name'], customID
                response = response.json()
                raise Exception('Failed to create application port profile {} '.format(response['message']))
        except Exception:
            raise

    @isSessionExpired
    def createNatRuleTask(self, payloadData, url):
        """
            Description :   Create NAT rule task
            Parameters  :   payloadData - payload data
                            url - NAT rule task URL (STRING)
        """
        try:
            if payloadData['ruleType'] == 'NO_SNAT':
                payloadData['externalAddresses'] = ''
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            # post api call to configure nat services on target edge gateway
            response = self.restClientObj.post(url, self.headers, data=json.dumps(payloadData))
            if response.status_code == requests.codes.accepted:
                # successful configuration of nat services on target edge gateway
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                if payloadData['id'] != '':
                    return payloadData['id']
                else:
                    return payloadData['name']
            else:
                # failed to configure nat services on target edge gateway
                response = response.json()
                raise Exception('Failed to configure configure NAT on Target {} '.format(response['message']))
        except Exception:
            raise

    @description("configuration of Target NAT")
    @remediate
    def configureTargetNAT(self, noSnatDestSubnet=None):
        """
        Description :   Configure the NAT service to the Target Gateway
        Parameters  :   noSnatDestSubnet    -   destimation subnet address (OPTIONAL)
        """
        try:
            targetEdgeGateway = copy.deepcopy(self.rollback.apiData['targetEdgeGateway'])
            logger.debug('NAT rules are getting configured')
            for sourceEdgeGateway in self.rollback.apiData['sourceEdgeGateway']:
                logger.debug("Configuring NAT Services in Target Edge Gateway - {}".format(sourceEdgeGateway['name']))
                sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
                t1gatewayId = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'], targetEdgeGateway))[0]['id']
                data = self.getEdgeGatewayNatConfig(sourceEdgeGatewayId, validation=False)
                # checking whether NAT rule is enabled or present in the source org vdc
                if not data or not data['enabled']:
                    logger.debug('NAT is not configured or enabled on Target Edge Gateway - {}'.format(sourceEdgeGateway['name']))
                    return
                if data['natRules']:
                    # get details of static routing config
                    staticRoutingConfig = self.getStaticRoutesDetails(sourceEdgeGatewayId)
                    # get details of BGP configuration
                    bgpConfigDetails = self.getEdgegatewayBGPconfig(sourceEdgeGatewayId, validation=False)
                    #get routing config details
                    routingConfigDetails = self.getEdgeGatewayRoutingConfig(sourceEdgeGatewayId, validation=False)
                    # get details of all Non default gateway subnet, default gateway and noSnatRules
                    allnonDefaultGatewaySubnetList, defaultGatewayDict, noSnatRulesList = self.getEdgeGatewayAdminApiDetails(
                        sourceEdgeGatewayId, staticRouteDetails=staticRoutingConfig)
                    natRuleList = data['natRules']['natRule']
                    # checking natrules is a list if not converting it into a list
                    sourceNATRules = natRuleList if isinstance(natRuleList, list) else [natRuleList]
                    url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_EDGE_GATEWAYS,
                                          vcdConstants.T1_ROUTER_NAT_CONFIG.format(t1gatewayId))
                    version = data['version']
                    applicationPortProfilesList = self.getApplicationPortProfiles()
                    userDefinedNAT = [natrule for natrule in sourceNATRules if natrule['ruleType'] == 'user']
                    # if source NAT is enabled NAT rule congiguration starts
                    statusForNATConfiguration = self.rollback.apiData.get('NATstatus', {})
                    rulesConfigured = statusForNATConfiguration.get(t1gatewayId, [])
                    if data['enabled'] == 'true':
                        for sourceNATRule in userDefinedNAT:
                            destinationIpDict = dict()
                            # checking whether 'ConfigStatus' key is present or not if present skipping that rule while remediation
                            if sourceNATRule['ruleId'] in rulesConfigured or sourceNATRule['ruleTag'] in rulesConfigured:
                                logger.debug('Rule Id: {} already created'.format(sourceNATRule['ruleId']))
                                continue
                            # loop to iterate over all subnets to check if translatedAddr in source rule does not belong
                            # to default gateway and is matches to primaryIp or subAllocated IP address
                            for eachParticipant in allnonDefaultGatewaySubnetList:
                                if eachParticipant['ipRanges'] is not None:
                                    for ipRange in eachParticipant['ipRanges']['ipRange']:
                                        participantStartAddr = ipRange['startAddress']
                                        participantEndAddr = ipRange['endAddress']
                                elif eachParticipant['ipRanges'] is None or eachParticipant['ipRanges'] == []:
                                    participantStartAddr = participantEndAddr = eachParticipant['ipAddress']
                                # check if translatedAddress belongs to suballocated address pool or primary IP
                                if self.ifIpBelongsToIpRange(sourceNATRule['translatedAddress'], participantStartAddr, participantEndAddr) \
                                        is True or sourceNATRule['translatedAddress'] == eachParticipant['ipAddress']:
                                    destinationIpDict = {'gateway': eachParticipant['gateway'],
                                                            'netmask': eachParticipant['netmask']}
                                    break
                            payloadData = self.createNATPayloadData(sourceNATRule, applicationPortProfilesList, version,
                                                                    defaultGatewayDict, destinationIpDict, noSnatRulesList,
                                                                    bgpConfigDetails, routingConfigDetails, noSnatDestSubnet)
                            payloadData = payloadData if isinstance(payloadData, list) else [payloadData]
                            for eachPayloadData in payloadData:
                                currentRuleId = self.createNatRuleTask(eachPayloadData, url)
                                # adding a key here to make sure the rule have configured successfully and when remediation skipping this rule
                                rulesConfigured.append(currentRuleId)
                                statusForNATConfiguration.update({t1gatewayId: rulesConfigured})
                                self.rollback.apiData['NATstatus'] = statusForNATConfiguration
                    else:
                        logger.debug('No NAT rules configured in Source Edge Gateway - {}'.format(sourceEdgeGateway['name']))
            logger.debug('NAT rules configured successfully on target')
        except Exception:
            raise
        finally:
            self.saveMetadataInOrgVdc()

    @description("configuration of BGP")
    @remediate
    def configBGP(self):
        """
        Description :   Configure BGP on the Target Edge Gateway
        """
        try:
            logger.debug('BGP is getting configured')
            for sourceEdgeGateway in self.rollback.apiData['sourceEdgeGateway']:
                logger.debug("Configuring BGP Services in Target Edge Gateway - {}".format(sourceEdgeGateway['name']))
                sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
                edgeGatewayID = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'],
                                     self.rollback.apiData['targetEdgeGateway']))[0]['id']

                bgpConfigDict = self.getEdgegatewayBGPconfig(sourceEdgeGatewayId, validation=False)
                data = self.getEdgeGatewayRoutingConfig(sourceEdgeGatewayId, validation=False)
                # checking whether bgp rule is enabled or present in the source edge  gateway; returning if no bgp in source edge gateway
                if not isinstance(bgpConfigDict, dict) or bgpConfigDict['enabled'] == 'false':
                    logger.debug('BGP service is disabled or not configured in Source Edge Gateway - {}'.format(sourceEdgeGateway['name']))
                    return
                logger.debug('BGP is getting configured in Source Edge Gateway - {}'.format(sourceEdgeGateway['name']))
                ecmp = "true" if data['routingGlobalConfig']['ecmp'] == "true" else "false"
                # url to get the details of the bgp configuration on T1 router i.e target edge gateway
                bgpurl = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                         vcdConstants.T1_ROUTER_BGP_CONFIG.format(edgeGatewayID))
                # get api call to retrieve the T1 router bgp details
                versionresponse = self.restClientObj.get(bgpurl, self.headers)
                if versionresponse.status_code == requests.codes.ok:
                    versionresponseDict = json.loads(versionresponse.content)
                    version = versionresponseDict['version']['version']
                else:
                    version = 1
                # creating payload to configure bgp
                bgpPayloaddict = {
                    "enabled": bgpConfigDict['enabled'],
                    "ecmp": ecmp,
                    "localASNumber": bgpConfigDict['localASNumber'],
                    "gracefulRestart": {

                    },
                    "version": {
                        "version": version
                    }
                }
                if bgpConfigDict['gracefulRestart'] != "true":
                    bgpPayloaddict['gracefulRestart']['mode'] = "DISABLE"
                bgpPayloaddata = json.dumps(bgpPayloaddict)
                self.headers['Content-Type'] = 'application/json'
                # put api call to configure bgp on target edge gateway
                response = self.restClientObj.put(bgpurl, self.headers, data=bgpPayloaddata)
                if response.status_code == requests.codes.accepted:
                    # successful configuration of bgp services on target edge gateway
                    taskUrl = response.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('BGP configuration updated successfully.')
                else:
                    # failure in configuring bgp on target edge gateway
                    response = response.json()
                    raise Exception('Failed to configure BGP in Source Edge Gateway {} - {}'
                                    .format(sourceEdgeGateway['name'], response['message']))
                # checking if bgp neighbours exist in source edge gateway; else returning
                if bgpConfigDict.get('bgpNeighbours'):
                    bgpNeighbours = bgpConfigDict['bgpNeighbours']['bgpNeighbour'] if isinstance(bgpConfigDict['bgpNeighbours']['bgpNeighbour'], list) else [bgpConfigDict['bgpNeighbours']['bgpNeighbour']]
                    self.createBGPNeighbours(bgpNeighbours, edgeGatewayID)
                    logger.debug('Successfully configured BGP in Source Edge Gateway - {}'.format(sourceEdgeGateway['name']))
                else:
                    logger.debug('No BGP neighbours configured in source BGP')
                    return
        except Exception:
            raise


    @description("configuration of DNS")
    @remediate
    def configureDNS(self):
        """
        Description : Configure DNS on specified edge gateway
        Parameters : edgeGatewayID - source edge gateway ID (STRING)
        """
        try:
            logger.debug('DNS is getting configured')
            self.rollback.apiData['listenerIp'] = {}
            for sourceEdgeGateway in self.rollback.apiData['sourceEdgeGateway']:
                sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
                edgeGatewayID = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'],
                                     self.rollback.apiData['targetEdgeGateway']))[0]['id']

                data = self.getEdgeGatewayDnsConfig(sourceEdgeGatewayId, validation=False)
                # configure dns on target only if source dns is enabled
                if data:
                    logger.debug('Configuring DNS on target edge gateway - {}'.format(sourceEdgeGateway['name']))
                    if isinstance(data, list):
                        forwarders = [forwarder['ipAddress'] for forwarder in data]
                    elif isinstance(data, OrderedDict):
                        forwarders = data['ipAddress'] if isinstance(data['ipAddress'], list) else [data['ipAddress']]
                    else:
                        forwardersList = [data]
                        forwarders = [forwarder['ipAddress'] for forwarder in forwardersList]
                    # creating payload for dns configuration
                    payloadData = {"enabled": True,
                                   "listenerIp": None,
                                   "defaultForwarderZone":
                                       {"displayName": "Default",
                                        "upstreamServers": forwarders},
                                   "conditionalForwarderZones": None,
                                   "version": None}
                    payloadData = json.dumps(payloadData)
                    # creating url for dns config update
                    url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_EDGE_GATEWAYS,
                                          vcdConstants.CREATE_DNS_CONFIG.format(edgeGatewayID))
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to configure dns
                    apiResponse = self.restClientObj.put(url, headers=self.headers, data=payloadData)
                    if apiResponse.status_code == requests.codes.accepted:
                        # successful configuration of dns
                        task_url = apiResponse.headers['Location']
                        self._checkTaskStatus(taskUrl=task_url)
                        logger.debug('DNS service configured successfully on target edge gateway - {}'.format(sourceEdgeGateway['name']))
                        url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                              vcdConstants.ALL_EDGE_GATEWAYS,
                                              vcdConstants.CREATE_DNS_CONFIG.format(edgeGatewayID))
                        self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                        # get api call to get dns listener ip
                        response = self.restClientObj.get(url, headers=self.headers)
                        if response.status_code == requests.codes.ok:
                            responseDict = response.json()
                            logger.warning(
                                "Use this Listener IP address {} when configuring VM's DNS server. The Org VDC network's"
                                " DNS server will be configured with this listener IP".format(responseDict['listenerIp']))
                            self.rollback.apiData['listenerIp'][edgeGatewayID] = responseDict['listenerIp']
                    else:
                        # failure in configuring dns
                        errorResponse = apiResponse.json()
                        raise Exception('Failed to configure DNS on target edge gateway {} - {} '
                                        .format(sourceEdgeGateway['name'], errorResponse['message']))
        except:
            raise

    @remediate
    def connectionPropertiesConfig(self, edgeGatewayID, ipsecConfigDict):
        """
        Description : Configuring Connection properties for IPSEC rules
        Parameters : edgeGatewayID - source edge gateway ID (STRING)
        """
        try:
            if not ipsecConfigDict or not ipsecConfigDict['enabled']:
                logger.debug('IPSec is not enabled or configured on source Org VDC.')
                return
            # url to retrive the ipsec rules on target edge gateway
            url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                  vcdConstants.T1_ROUTER_IPSEC_CONFIG.format(edgeGatewayID))
            ipsecRulesResponse = self.restClientObj.get(url, self.headers)
            if ipsecRulesResponse.status_code == requests.codes.ok:
                ipsecrules = json.loads(ipsecRulesResponse.content)
                ipesecrules = ipsecrules['values'] if isinstance(ipsecrules['values'], list) else [ipsecrules]
                sourceIPsecSites = ipsecConfigDict['sites']['sites'] if isinstance(ipsecConfigDict['sites']['sites'], list) else [ipsecConfigDict['sites']['sites']]
                for sourceIPsecSite in sourceIPsecSites:
                    for ipsecrule in ipesecrules:
                        if ipsecrule['name'] == sourceIPsecSite['name']:
                            ruleid = ipsecrule['id']
                            # checking whether 'ConfigStatus' key is present or not if present skipping that rule while remediation
                            if self.rollback.apiData.get(ruleid):
                                continue
                            propertyUrl = "{}{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                                            vcdConstants.T1_ROUTER_IPSEC_CONFIG.format(edgeGatewayID), vcdConstants.CONNECTION_PROPERTIES_CONFIG.format(ruleid))
                            # if the source encryption algorithm is 'AES-GCM', then target Ike algorith supported is 'AES 128'
                            if sourceIPsecSite['encryptionAlgorithm'] == 'aes-gcm':
                                ikeEncryptionAlgorithm  = vcdConstants.CONNECTION_PROPERTIES_ENCRYPTION_ALGORITHM.get('aes')
                                tunnelDigestAlgorithm = None
                                ikeDigestAlgorithm = vcdConstants.CONNECTION_PROPERTIES_DIGEST_ALGORITHM.get('sha1')
                            else:
                                ikeEncryptionAlgorithm = vcdConstants.CONNECTION_PROPERTIES_ENCRYPTION_ALGORITHM.get(sourceIPsecSite['encryptionAlgorithm'])
                                tunnelDigestAlgorithm = [vcdConstants.CONNECTION_PROPERTIES_DIGEST_ALGORITHM.get(sourceIPsecSite['digestAlgorithm'])]
                                ikeDigestAlgorithm = vcdConstants.CONNECTION_PROPERTIES_DIGEST_ALGORITHM.get(sourceIPsecSite['digestAlgorithm'])
                            payloadDict = {
                                    "securityType": "CUSTOM",
                                    "ikeConfiguration": {
                                        "ikeVersion": vcdConstants.CONNECTION_PROPERTIES_IKE_VERSION.get(sourceIPsecSite['ikeOption']),
                                        "dhGroups": [vcdConstants.CONNECTION_PROPERTIES_DH_GROUP.get(sourceIPsecSite['dhGroup'])],
                                        "digestAlgorithms": [ikeDigestAlgorithm],
                                        "encryptionAlgorithms": [ikeEncryptionAlgorithm],
                                    },
                                    "tunnelConfiguration": {
                                        "perfectForwardSecrecyEnabled": "true" if sourceIPsecSite['enablePfs'] else "false",
                                        "dhGroups": [vcdConstants.CONNECTION_PROPERTIES_DH_GROUP.get(sourceIPsecSite['dhGroup'])],
                                        "encryptionAlgorithms": [vcdConstants.CONNECTION_PROPERTIES_ENCRYPTION_ALGORITHM.get(sourceIPsecSite['encryptionAlgorithm'])],
                                        "digestAlgorithms": tunnelDigestAlgorithm
                                    }
                                }
                            payloadData = json.dumps(payloadDict)
                            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                            # put api call to configure dns
                            apiResponse = self.restClientObj.put(propertyUrl, headers=self.headers, data=payloadData)
                            if apiResponse.status_code == requests.codes.accepted:
                                # successfully configured connection property of ipsec
                                task_url = apiResponse.headers['Location']
                                self._checkTaskStatus(taskUrl=task_url)
                                # adding a key here to make sure the rule have configured successfully and when remediation skipping this rule
                                self.rollback.apiData[ruleid] = True
                                logger.debug('Connection properties successfully configured for ipsec rule {}'.format(ipsecrule['name']))
                            else:
                                # failure in configuring ipsec configuration properties
                                errorResponse = apiResponse.json()
                                raise Exception('Failed to configure connection properties for ipsec rule {} with errors - {} '.format(ipsecrule['name'], errorResponse['message']))
        except Exception:
            raise

    def createSecurityGroup(self, networkID, firewallRule, edgeGatewayID):
        """
           Description: Create security groups in the target Edge gateway
           Paramater: networkID: ID of Org VDC network
                      firewallRule: Details of firewall rule
                      edgeGatewayID: Edgegateway ID
                      targetOrgVDCId - ID of target org vdc (STRING)
        """
        try:
            # taking target edge gateway id from apioutput json file
            targetOrgVdcId = self.rollback.apiData['targetOrgVDC']['@id']
            target_networks = self.retrieveNetworkListFromMetadata(targetOrgVdcId, orgVDCType='target')
            networkgroups = networkID
            firewallRule = firewallRule
            edgeGatewayId = edgeGatewayID
            firewallGroupIds = []
            groupId = []
            newMembers = []
            allGroupMembers = []
            newFirewallGroupIds = []
            # getting the network details for the creation of the firewall group
            members = list()
            logger.debug('Configuring security group for firewall {}.'.format(firewallRule['id']))
            for networkgroup in networkgroups:
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.GET_ORG_VDC_NETWORK_BY_ID.format(networkgroup))
                getnetworkResponse = self.restClientObj.get(url, self.headers)
                if getnetworkResponse.status_code == requests.codes.ok:
                    responseDict = json.loads(getnetworkResponse.content)
                    for target_network in target_networks:
                        if responseDict['name']+'-v2t' == target_network['name']:
                            network_name = target_network['name']
                            network_id = target_network['id']
                            members.append({'name': network_name, 'id': network_id})
            # getting the already created firewall groups summaries
            summaryValues = self.fetchFirewallGroups()
            for summary in summaryValues:
                # checking if the firewall group is already created for the given edge gateway
                # if yes then appending the firewall group id to the list
                if summary.get('edgeGatewayRef'):
                    if summary['edgeGatewayRef']['id'] == edgeGatewayId:
                        firewallGroupIds.append(summary['id'])
            for firewallGroupId in firewallGroupIds:
                # getting the details of specific firewall group
                groupIdUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.FIREWALL_GROUP.format(firewallGroupId))
                getGroupResponse = self.restClientObj.get(groupIdUrl, self.headers)
                if getGroupResponse.status_code == requests.codes.ok:
                    groupResponse = json.loads(getGroupResponse.content)
                    groupMembers = groupResponse['members']
                    if groupMembers:
                        # here appending all the group members to the list
                        for member in groupMembers:
                            allGroupMembers.append(member)
                            # for every group checking if both network and group members are same then appending it in list
                            if member in members:
                                groupId.append(firewallGroupId)
            for member in members:
                # validating if the network member doesn't exists in the members of the firewall groups which are already created
                # then adding it to the list which will be used for the creation of the new firewall group
                if member not in allGroupMembers:
                    newMembers.append(member)
            else:
                # if the newMembers list is empty then
                # return the id of that existing firewall group with same member present in it
                if not newMembers:
                    return groupId
                # else create the new firewall group
                else:
                    for member in newMembers:
                        # getting the new member name from the list
                        network_name = member['name'].split('-', -1)
                        # popping out '-v2t' from the name fetched above
                        network_name.pop(-1)
                        # joining the remaining substrings
                        network_name = '-'.join(network_name)
                        # creating payload data to create firewall group
                        firewallGroupDict = {'name': 'SecurityGroup-(' + network_name + ')'}
                        if self.rollback.apiData.get(firewallGroupDict['name']):
                            continue
                        firewallGroupDict['edgeGatewayRef'] = {'id': edgeGatewayId}
                        firewallGroupDict['members'] = [member]
                        firewallGroupDict['type'] = vcdConstants.SECURITY_GROUP
                        firewallGroupData = json.dumps(firewallGroupDict)
                        # url to create firewall group
                        firewallGroupUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                         vcdConstants.CREATE_FIREWALL_GROUP)
                        self.headers['Content-Type'] = 'application/json'
                        # post api call to create firewall group
                        response = self.restClientObj.post(firewallGroupUrl, self.headers,data=firewallGroupData)
                        if response.status_code == requests.codes.accepted:
                            # successful creation of firewall group
                            taskUrl = response.headers['Location']
                            firewallGroupId = self._checkTaskStatus(taskUrl=taskUrl, returnOutput=True)
                            self.rollback.apiData[firewallGroupDict['name']] = True
                            logger.debug('Successfully configured security group for firewall {}.'.format(firewallRule['id']))
                            # appending the new firewall group id
                            newFirewallGroupIds.append('urn:vcloud:firewallGroup:{}'.format(firewallGroupId))
                        else:
                            # failure in creation of firewall group
                            response = response.json()
                            raise Exception('Failed to create Security Group - {}'.format(response['message']))
                    # returning the firewall group ids list
                    return newFirewallGroupIds
        except Exception:
            raise

    @remediate
    def createBGPNeighbours(self, bgpNeighbours, edgeGatewayID):
        """
        Description: create BGP Neighbours in target edge gateway
        parameters: edgeGatewayID: edgegateway ID
                    bgpNeighbours: list of bgpNeighbours
        """
        try:
            # iterating over the source edge gateway's bgp neighbours
            for bgpNeighbour in bgpNeighbours:
                # if configStatus flag is already set means that the bgpNeighbor rule is already configured, if so then skipping the configuring of same rule and moving to the next bgpNeighbour rule
                if self.rollback.apiData.get(bgpNeighbour['ipAddress']):
                    continue
                # creating payload to configure same bgp neighbours in target edge gateway as those in source edge gateway
                bgpNeighbourpayloadDict = {
                    "neighborAddress": bgpNeighbour['ipAddress'],
                    "remoteASNumber": bgpNeighbour['remoteASNumber'],
                    "keepAliveTimer": int(bgpNeighbour['keepAliveTimer']),
                    "holdDownTimer": int(bgpNeighbour['holdDownTimer']),
                    "allowASIn": "false",
                    "neighborPassword": bgpNeighbour['password'] if bgpNeighbour.get('password') else ''
                }
                # checking for the bgp filters
                if bgpNeighbour.get("bgpFilters"):
                    # retrieving the list instance of bgp filters of source edge gateway
                    bgpFilters = bgpNeighbour['bgpFilters']['bgpFilter'] if isinstance(
                        bgpNeighbour['bgpFilters']['bgpFilter'], list) else [bgpNeighbour['bgpFilters']['bgpFilter']]
                    infilters = [bgpFilter for bgpFilter in bgpFilters if bgpFilter['direction'] == 'in']
                    outfilter = [bgpFilter for bgpFilter in bgpFilters if bgpFilter['direction'] == 'out']
                    if infilters:
                        inRoutesFilterRef = self.createBGPFilters(bgpFilters=infilters, edgeGatewayID=edgeGatewayID, filtertype='in', bgpNeighbour=bgpNeighbour)
                        bgpNeighbourpayloadDict['inRoutesFilterRef'] = inRoutesFilterRef if inRoutesFilterRef else ''
                    if outfilter:
                        outRoutesFilterRef = self.createBGPFilters(bgpFilters=outfilter, edgeGatewayID=edgeGatewayID, filtertype='out', bgpNeighbour=bgpNeighbour)
                        bgpNeighbourpayloadDict['outRoutesFilterRef'] = outRoutesFilterRef if outRoutesFilterRef else ''
                # time.sleep put bcoz prefix list still takes time after creation and the api response is success
                time.sleep(5)
                bgpNeighbourpayloadData = json.dumps(bgpNeighbourpayloadDict)
                # url to configure bgp neighbours
                bgpNeighboururl = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                  vcdConstants.ALL_EDGE_GATEWAYS,
                                                  vcdConstants.CREATE_BGP_NEIGHBOR_CONFIG.format(edgeGatewayID))
                self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                # post api call to configure bgp neighbours
                bgpNeighbourresponse = self.restClientObj.post(bgpNeighboururl, headers=self.headers,
                                                               data=bgpNeighbourpayloadData)
                if bgpNeighbourresponse.status_code == requests.codes.accepted:
                    # successful configuration of bgp neighbours
                    task_url = bgpNeighbourresponse.headers['Location']
                    self._checkTaskStatus(taskUrl=task_url)
                    # setting the configStatus flag meaning the particular bgpNeighbour rule is configured successfully in order to skip its reconfiguration
                    self.rollback.apiData[bgpNeighbour['ipAddress']] = True
                    logger.debug('BGP neighbor created successfully')
                else:
                    # failure in configuring bgp neighbours
                    bgpNeighbourresponse = bgpNeighbourresponse.json()
                    raise Exception('Failed to create neighbors {} '.format(bgpNeighbourresponse['message']))
            logger.debug('BGP neighbors configured successfully')
        except Exception:
            raise

    @isSessionExpired
    def createBGPFilters(self, bgpFilters, edgeGatewayID, filtertype, bgpNeighbour):
        """
        Description: Create BGP in-filters and out-filters
        parameters: bgpfilters: in and out filters of bgp neighbours
                    edgeGatewayID: ID of edgegateway
                    filtertype: in/out
                    bgpNeighbour: details of BGP Neighbour
        """
        try:
            FilterpayloadDict = {'prefixes': []}
            # iterating over the bgp filters
            for bgpFilter in bgpFilters:
                FilterpayloadDict['prefixes'].append({
                    "network": bgpFilter['network'], "action": bgpFilter['action'].upper(),
                    "greaterThanEqualTo": bgpFilter['ipPrefixGe'] if 'ipPrefixGe' in bgpFilter.keys() else "",
                    "lessThanEqualTo": bgpFilter['ipPrefixLe'] if 'ipPrefixLe' in bgpFilter.keys() else ""})
            if filtertype == "in":
                FilterpayloadDict['name'] = bgpNeighbour['ipAddress'] + "-" + "IN-" + str(random.randint(1, 1000))
            elif filtertype == "out":
                FilterpayloadDict['name'] = bgpNeighbour['ipAddress'] + "-" + "OUT-" + str(random.randint(1, 1000))
            FilterpayloadDict = json.dumps(FilterpayloadDict)
            # url to configure in direction filtered bgp services
            infilterurl = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_EDGE_GATEWAYS,
                                          vcdConstants.CREATE_PREFIX_LISTS_BGP.format(edgeGatewayID))
            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
            # post api call to configure in direction filtered bgp services
            infilterresponse = self.restClientObj.post(infilterurl, headers=self.headers,
                                                       data=FilterpayloadDict)
            if infilterresponse.status_code == requests.codes.accepted:
                # successful configuration of in direction filtered bgp services
                taskUrl = infilterresponse.headers['Location']
                FilterpayloadDict = json.loads(FilterpayloadDict)
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug('Successfully created BGP filter {}'.format(FilterpayloadDict['name']))
                # get api call to retrieve
                inprefixListResponse = self.restClientObj.get(infilterurl, self.headers)
                inprefixList = inprefixListResponse.json()
                values = inprefixList['values']
                for value in values:
                    if FilterpayloadDict['name'] == value['name']:
                        routesFilterRef = {"id": value['id'], "name": value['name']}
                        return routesFilterRef
            else:
                infilterresponseData = infilterresponse.json()
                raise Exception('Failed to create BGP filters {}'.format(infilterresponseData['message']))
        except Exception:
            raise

    def createNATPayloadData(self, sourceNATRule, applicationPortProfilesList, version,
                             defaultEdgeGateway, destinationIpDict, staticRoutesList, bgpDetails,
                             routingConfigDetails, noSnatDestSubnetList = None):
        """
                Description :   Creates the payload data for the NAT service to the Target Gateway
                Parameters  :   sourceNATRule   -   NAT Rule of source gateway  (DICT)
                                applicationPortProfilesList   -   Application Port Profiles  (LIST)
                                version         -   version
                                defaultEdgeGateway   -   default edge gateway details
                                destinationIpDict   -   destination IP dict
                                staticRoutesList    -   static rule configuration (LIST)
                                bgpDetails  -   BGP configuration details
                                routingConfigDetails - Edge gateway routing config
                                noSnatDestSubnetList    -   NoSNAT destination subnet from sample input
        """
        # creating common payload dict for both DNAT AND SNAT
        payloadDict = {
            "ruleId": sourceNATRule['ruleId'],
            "ruleTag": sourceNATRule['ruleTag'],
            "ruleDescription": sourceNATRule['description'] if sourceNATRule.get('description') else '',
            "enabled": "true" if sourceNATRule['enabled'] == "true" else "false",
            "action": sourceNATRule['action'].upper(),
            "loggingEnabled": "true" if sourceNATRule['loggingEnabled'] == "true" else "false",
            "version": version
        }
        # configuring DNAT
        if sourceNATRule['action'] == "dnat":
            translatedAddressCIDR = sourceNATRule['translatedAddress']
            # updating payload dict
            payloadDict.update({
                "originalAddress": sourceNATRule['originalAddress'],
                "translatedAddress": translatedAddressCIDR
            })
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
            # creating payload data
            payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CREATE_DNAT_TEMPLATE, apiVersion=self.version)
            payloadData = json.loads(payloadData)
            # adding dnatExternalPort port profile to payload data
            if float(self.version) <= float(vcdConstants.API_VERSION_PRE_ZEUS):
                payloadData["internalPort"] = sourceNATRule['originalPort'] if sourceNATRule['originalPort'] != 'any' else ''
            else:
                payloadData["dnatExternalPort"] = sourceNATRule['originalPort'] if sourceNATRule['originalPort'] != 'any' else ''

            # From VCD v10.2.2, firewallMatch to external address to be provided for DNAT rules
            if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS_10_2_2):
                payloadData["firewallMatch"] = "MATCH_EXTERNAL_ADDRESS"

            # if protocol and port is not equal to any search or creating new application port profiles
            if sourceNATRule['protocol'] != "any" and sourceNATRule['translatedPort'] != "any":
                protocol_port_name, protocol_port_id = self._searchApplicationPortProfile(
                    applicationPortProfilesList, sourceNATRule['protocol'], sourceNATRule['translatedPort'])
                payloadData["applicationPortProfile"] = {"name": protocol_port_name, "id": protocol_port_id}
            # checking the protocol is icmp
            elif sourceNATRule['protocol'] == "icmp":
                # checking the icmptype is any
                if sourceNATRule['icmpType'] == "any":
                    for value in applicationPortProfilesList:
                        if value['name'] == vcdConstants.ICMP_ALL:
                            protocol_port_name, protocol_port_id = value['name'], value['id']
                            payloadData["applicationPortProfile"] = {"name": protocol_port_name,
                                                                     "id": protocol_port_id}
                else:
                    # getting icmp port profiles
                    icmpurl = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                              vcdConstants.APPLICATION_PORT_PROFILES,
                                              vcdConstants.GET_ICMP_PORT_PROFILES_FILTER)
                    icmpResponse = self.restClientObj.get(icmpurl, self.headers)
                    if icmpResponse.status_code == requests.codes.ok:
                        icmpresponseDict = icmpResponse.json()
                        icmpvalues = icmpresponseDict['values']
                        # iterating through icmp values
                        for icmpvalue in icmpvalues:
                            # checking if icmp type is not any and icmp port is not ICMPv4-ALL
                            if sourceNATRule['icmpType'] != "any" and "-" not in icmpvalue['name']:
                                start = sourceNATRule['icmpType'].split('-')[0]
                                end = sourceNATRule['icmpType'].split('-')[-1]
                                sourceIcmpType = start + " " + end
                                protocol_Name = icmpvalue['name'].split("ICMP")
                                protocol_Name_id = "".join(protocol_Name)
                                protocol_Name_id = protocol_Name_id.strip()
                                # checking the source icmp type and target icmp type by converting it into upper
                                if sourceIcmpType.upper() == protocol_Name_id.upper():
                                    protocol_port_name, protocol_port_id = icmpvalue['name'], icmpvalue['id']
                                    payloadData['applicationPortProfile'] = {"name": protocol_port_name,
                                                                             "id": protocol_port_id}
                                    break
                                # checking source icmp redirect type and icmp redirect port profile
                                if sourceNATRule['icmpType'] == "redirect" and icmpvalue['name'] == "ICMP Redirect":
                                    protocol_port_name, protocol_port_id = icmpvalue['name'], icmpvalue[
                                        'id']
                                    payloadData["applicationPortProfile"] = {"name": protocol_port_name,
                                                                             "id": protocol_port_id}
                                    break
                            # for the icmp type which is not present in port profiles, will taking it as ICMPv4-ALL
                            elif icmpvalue['name'] == vcdConstants.ICMP_ALL:
                                protocol_port_name, protocol_port_id = icmpvalue['name'], icmpvalue['id']
                                payloadData["applicationPortProfile"] = {"name": protocol_port_name,
                                                                         "id": protocol_port_id}
                                break
            else:
                payloadData["applicationPortProfile"] = None
        # configuring SNAT
        if sourceNATRule['action'] == "snat":
            allSnatPayloadList = list()
            payloadDataList = list()
            ifbgpRouterIdAddress = bool()
            # if range present in source orginal address converting it into cidr
            if "-" in sourceNATRule['originalAddress']:
                translatedAddressCIDR = self.cidrCalculator(sourceNATRule['originalAddress'])
            else:
                translatedAddressCIDR = sourceNATRule['originalAddress']
            payloadDict.update({
                "originalAddress": sourceNATRule['translatedAddress'],
                "translatedAddress": translatedAddressCIDR
            })
            ipInSuAllocatedStatus = False
            if defaultEdgeGateway is {}:
                raise Exception('Default Gateway not configured on Edge Gateway')
            # If dynamic routerId belongs to default gateway subnet
            if isinstance(bgpDetails, dict):
                networkAddress = ipaddress.IPv4Network('{}/{}'.format(defaultEdgeGateway['gateway'],
                                                                           defaultEdgeGateway['subnetPrefixLength']),
                                                       strict=False)
                ifbgpRouterIdAddress = ipaddress.ip_address(routingConfigDetails['routingGlobalConfig']['routerId']) in \
                                       ipaddress.ip_network(networkAddress)

            # bgpRouterIdAddress = False
            for eachIpRange in defaultEdgeGateway['ipRanges']:
                startAddr, endAddr = eachIpRange.split('-')
                # if translated IP address belongs to default gateway Ip range return True
                ipInSuAllocatedStatus = self.ifIpBelongsToIpRange(sourceNATRule['translatedAddress'],
                                                                  startAddr, endAddr)
                if ipInSuAllocatedStatus:
                    if staticRoutesList:
                        for eachNetwork in staticRoutesList:
                            staticNoSnatPayloadDict = copy.deepcopy(payloadDict)
                            staticNoSnatPayloadDict['ruleId'] = ''
                            staticNoSnatPayloadDict['ruleTag'] = 'staticRouteNoSnat' + payloadDict['ruleTag']
                            staticNoSnatPayloadDict['action'] = 'NO_SNAT'
                            staticNoSnatPayloadDict['snatDestinationAddresses'] = eachNetwork
                            allSnatPayloadList.append(staticNoSnatPayloadDict)
                    if noSnatDestSubnetList is not None and isinstance(bgpDetails, dict) and \
                            bgpDetails['enabled'] == 'true' and ifbgpRouterIdAddress == False:
                        noSnatDestSubnetList = noSnatDestSubnetList if isinstance(noSnatDestSubnetList, list) else [noSnatDestSubnetList]
                        for eachExtNetwork in noSnatDestSubnetList:
                            bgpNoSnatPayloadDict = copy.deepcopy(payloadDict)
                            bgpNoSnatPayloadDict['ruleId'] = ''
                            bgpNoSnatPayloadDict['ruleTag'] = 'bgpNoSnat-' + payloadDict['ruleTag']
                            bgpNoSnatPayloadDict['action'] = 'NO_SNAT'
                            bgpNoSnatPayloadDict['snatDestinationAddresses'] = eachExtNetwork
                            allSnatPayloadList.append(bgpNoSnatPayloadDict)
            # iftranslated IP address does not belongs to default gateway update snatDestinationAddresses
            if ipInSuAllocatedStatus == False and destinationIpDict != {}:
                networkAddr = ipaddress.ip_network('{}/{}'.format(destinationIpDict['gateway'],
                                                                  destinationIpDict['netmask']),
                                                   strict=False)
                payloadDict.update({'snatDestinationAddresses': networkAddr.compressed})
            else:
                payloadDict.update({'snatDestinationAddresses': ''})
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
            # creating payload data
            allSnatPayloadList.append(payloadDict)
            for eachDict in allSnatPayloadList:
                payloadData = self.vcdUtils.createPayload(filePath, eachDict, fileType='json',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CREATE_SNAT_TEMPLATE, apiVersion=self.version)
                payloadDataList.append(json.loads(payloadData))
            if payloadDataList:
                return payloadDataList
        return payloadData

    @isSessionExpired
    def createIPSET(self, ipsetgroups, edgeGatewayId):
        """
        Description : Create IPSET as security group for firewall
        Parameters: ipsetgroups - All the IPset's information in Source Org VDC
                    edgeGatewayId - The id of the Target edge gateway(for NSX API so entity id)
        """
        try:
            if ipsetgroups:
                logger.debug('Creating IPSET in Target Edge Gateway')
                firewallGroupIds = list()
                firewallGroupName = list()
                firewallIdDict = dict()
                # iterating over the ipset group list
                for ipsetgroup in ipsetgroups:
                    ipAddressList = list()
                    # url to retrieve the info of ipset group by id
                    ipseturl = "{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                             vcdConstants.GET_IPSET_GROUP_BY_ID.format(ipsetgroup['objectId']))
                    # get api call to retrieve the ipset group info
                    ipsetresponse = self.restClientObj.get(ipseturl, self.headers)
                    if ipsetresponse.status_code == requests.codes.ok:
                        # successful retrieval of ipset group info
                        ipsetresponseDict = xmltodict.parse(ipsetresponse.content)
                        if self.rollback.apiData.get('firewallIdDict'):
                            if self.rollback.apiData['firewallIdDict'].get(edgeGatewayId):
                                if self.rollback.apiData['firewallIdDict'][edgeGatewayId].get(ipsetresponseDict['ipset']['name']):
                                    continue
                        # storing the ip-address and range present in the IPSET
                        ipsetipaddress = ipsetresponseDict['ipset']['value']

                        description = ipsetresponseDict['ipset']['description'] if ipsetresponseDict['ipset'].get(
                            'description') else ''
                        # if multiple ip=address or range present in the ipset spliting it with ','
                        if "," in ipsetipaddress:
                            ipsetipaddresslist = ipsetipaddress.split(',')
                            ipAddressList.extend(ipsetipaddresslist)
                        else:
                            ipAddressList.append(ipsetipaddress)
                        # creating payload data to create firewall group
                        firewallGroupDict = {'name': ipsetresponseDict['ipset']['name'], 'description': description,
                                             'edgeGatewayRef': {'id': edgeGatewayId}, 'ipAddresses': ipAddressList}
                        firewallGroupDict = json.dumps(firewallGroupDict)
                        # url to create firewall group
                        firewallGroupUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                         vcdConstants.CREATE_FIREWALL_GROUP)
                        self.headers['Content-Type'] = 'application/json'
                        # post api call to create firewall group
                        response = self.restClientObj.post(firewallGroupUrl, self.headers,
                                                           data=firewallGroupDict)
                        if response.status_code == requests.codes.accepted:
                            # successful creation of firewall group
                            taskUrl = response.headers['Location']
                            firewallGroupId = self._checkTaskStatus(taskUrl=taskUrl, returnOutput=True)
                            self.rollback.apiData[ipsetgroup['objectId'].split(':')[-1]] = 'urn:vcloud:firewallGroup:{}'.format(firewallGroupId)
                            firewallGroupIds.append({'id': 'urn:vcloud:firewallGroup:{}'.format(firewallGroupId)})
                            firewallGroupName.append(ipsetresponseDict['ipset']['name'])
                            # creating a dict with firewallName as key and firewallIDs as value
                            firewallIdDict = dict(zip(firewallGroupName, firewallGroupIds))
                        else:
                            errorResponse = response.json()
                            raise Exception('Failed to create IPSET - {}'.format(errorResponse['message']))
                    else:
                        errorResponse = ipsetresponse.json()
                        raise Exception('Failed to get IPSET details due to error - {}'.format(errorResponse['message']))
                if self.rollback.apiData.get('firewallIdDict'):
                    self.rollback.apiData['firewallIdDict'].update({edgeGatewayId: firewallIdDict})
                else:
                    self.rollback.apiData['firewallIdDict'] = {edgeGatewayId: firewallIdDict}
                logger.debug('Successfully configured IPSET in target')
                return firewallIdDict
        except Exception:
            raise

    @isSessionExpired
    def dhcpRollBack(self):
        """
        Description: Creating DHCP service in Source Org VDC for roll back
        """
        try:
            # Check if services configuration or network switchover was performed or not
            if not self.rollback.metadata.get("configureTargetVDC", {}).get("disconnectSourceOrgVDCNetwork"):
                return

            data = self.rollback.apiData['sourceEdgeGatewayDHCP']
            # ID of source edge gateway
            for sourceEdgeGatewayId in self.rollback.apiData['sourceEdgeGatewayId']:
                edgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
                # url for dhcp configuration
                url = "{}{}{}?async=true".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                      vcdConstants.NETWORK_EDGES,
                                      vcdConstants.EDGE_GATEWAY_DHCP_CONFIG_BY_ID .format(edgeGatewayId))
                # if DHCP pool was present in the source
                if data[sourceEdgeGatewayId]['ipPools']:
                    del data[sourceEdgeGatewayId]['version']
                    payloadData = json.dumps(data[sourceEdgeGatewayId])
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    if response.status_code == requests.codes.accepted:
                        # only need job ID from Location so spliting it
                        jobId = response.headers['Location'].split('/')[-1]
                        taskUrl = '{}{}{}'.format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress), vcdConstants.NETWORK_EDGES, vcdConstants.NSX_JOBS.format(jobId))
                        # initial time
                        timeout = 0.0
                        # polling till time exceeds
                        while timeout < vcdConstants.VCD_CREATION_TIMEOUT:
                            response = self.restClientObj.get(taskUrl, self.headers)
                            if response.status_code == requests.codes.ok:
                                responseDict = xmltodict.parse(response.content)
                                # checking for the status of each polling call for 'Completed'
                                if responseDict['edgeJob']['status'] == 'COMPLETED':
                                    logger.info('Rollback for dhcp is completed successfully')
                                    break
                                # checking if the task failed
                                if responseDict['edgeJob']['status'] == "FAILED":
                                    logger.debug("Failed configuring DHCP service in edge gateway {}".format(responseDict['edgeJob']['message']))
                                    raise Exception(responseDict['edgeJob']['message'])
        except Exception:
            raise

    @isSessionExpired
    def ipsecRollBack(self):
        """
        Description: Configuring IPSEC service in source Edge gateway for roll back
        """
        try:
            # Check if services configuration or network switchover was performed or not
            if not self.rollback.metadata.get("configureTargetVDC", {}).get("disconnectSourceOrgVDCNetwork"):
                return

            for sourceEdgeGateway in self.rollback.apiData['sourceEdgeGateway']:
                # ID of source edge gateway
                edgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
                data = self.rollback.apiData['ipsecConfigDict'][sourceEdgeGateway['name']]
                # url for ipsec configuration
                url = "{}{}{}&async=true".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                                 vcdConstants.NETWORK_EDGES,
                                                 vcdConstants.EDGE_GATEWAY_IPSEC_CONFIG.format(edgeGatewayId))
                if data['sites']:
                    del data['version']
                    for site in data['sites']['sites']:
                        del site['siteId']
                        if site.get('ConfigStatus'):
                            del site['ConfigStatus']
                    payloadData = json.dumps(data)
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    if response.status_code == requests.codes.accepted:
                        # only need job ID from Location so spliting it
                        jobId = response.headers['Location'].split('/')[-1]
                        taskUrl = '{}{}{}'.format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                                  vcdConstants.NETWORK_EDGES, vcdConstants.NSX_JOBS.format(jobId))
                        # initial time
                        timeout = 0.0
                        # polling till time exceeds
                        while timeout < vcdConstants.VCD_CREATION_TIMEOUT:
                            response = self.restClientObj.get(taskUrl, self.headers)
                            if response.status_code == requests.codes.ok:
                                responseDict = xmltodict.parse(response.content)
                                # checking for the status of each polling call for 'Completed'
                                if responseDict['edgeJob']['status'] == 'COMPLETED':
                                    logger.debug('Rollback for IPSEC is completed successfully in {}'.format(sourceEdgeGateway['name']))
                                    break
                                    # checking if the task failed
                                if responseDict['edgeJob']['status'] == "FAILED":
                                    logger.debug("Failed configuring IPSEC VPN in edge gateway {}".format(responseDict['edgeJob']['message']))
                                    raise Exception(responseDict['edgeJob']['message'])
        except Exception:
            raise

    @isSessionExpired
    def getCerticatesFromTenant(self):
        """
            Description :   Fetch the names of certificates present in tenant portal
            Returns     :   dictionary of names and ids of certificates present in tenant portal
        """
        try:
            logger.debug('Getting the certificates present in vCD tenant portal')
            url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.CERTIFICATE_URL)

            # updating headers for get request
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            self.headers['X-VMWARE-VCLOUD-TENANT-CONTEXT'] = self.rollback.apiData['Organization']['@id'].split(':')[-1]

            response = self.restClientObj.get(url=url, headers=self.headers)
            if response.status_code == requests.codes.ok:
                logger.debug('Successfully retrieved load balancer certificates')
                responseDict = response.json()
                # Retrieving certificate names from certificates
                certificateNameIdDict = {certificate['alias']: certificate['id'] for certificate in responseDict.get('values', [])}

                # removing tenant context header after retrieving certificate from tenant portal
                del self.headers['X-VMWARE-VCLOUD-TENANT-CONTEXT']

                return certificateNameIdDict
            else:
                errorResponseDict = response.json()
                raise Exception("Failed to retrieve certificates from vcd due to error - {}".format(errorResponseDict[
                                                                                                        'message']))
        except:
            raise


    @isSessionExpired
    def uploadCertificate(self, certificate, certificateName):
        """
        Description :   Upload the certificate for load balancer HTTPS configuration
        Params      :   certificate - certificate to be uploaded in vCD (STRING)
                        certificateName - name of certificate that if required (STRING)
        """
        try:
            logger.debug('Upload the certificate for load balancer HTTPS configuration')
            pkcs8PemFileName = 'privateKeyPKCS8.pem'
            url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.CERTIFICATE_URL)

            # reading pkcs8 format private key from file
            with open(pkcs8PemFileName, 'r', encoding='utf-8') as privateFile:
                privateKey = privateFile.read()

            payloadData = {
                "alias": certificateName,
                "privateKey": privateKey,
                "privateKeyPassphrase": "",
                "certificate": certificate,
                "description": ""
            }
            payloadData = json.dumps(payloadData)

            # updating headers for post request
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            self.headers['X-VMWARE-VCLOUD-TENANT-CONTEXT'] = self.rollback.apiData['Organization']['@id'].split(':')[-1]

            response = self.restClientObj.post(url=url, headers=self.headers, data=payloadData)
            if response.status_code == requests.codes.created:
                logger.debug('Successfully uploaded load balancer certificate')
            else:
                errorResponseDict = response.json()
                raise Exception("Failed to upload certificate '{}' in vcd due to error - {}".format(certificateName, errorResponseDict['message']))

            # removing tenant context header after uploding certificate to tenant portal
            del self.headers['X-VMWARE-VCLOUD-TENANT-CONTEXT']
        except:
            raise
        finally:
            # Removing the pem file afte operation
            if os.path.exists(pkcs8PemFileName):
                os.remove(pkcs8PemFileName)

    @isSessionExpired
    def getPoolSumaryDetails(self, edgeGatewayId):
        """
            Description :   Fetch details of load balancer pools of a edge gateway
            Parameters  :   edgeGatewayId - ID of edge gateway whose data is to be fetched
            Returns     :   List of load balancer pools of edge gateway(LIST)
        """
        try:
            url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.EDGE_GATEWAY_LOADBALANCER_POOLS_USING_ID.format(edgeGatewayId))

            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved load balancer pool details successfully")
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
            else:
                raise Exception('Failed to fetch load balancer pool details')
            pageNo = 1
            pageSizeCount = 0
            targetLoadBalancerPoolSummary = []
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.EDGE_GATEWAY_LOADBALANCER_POOLS_USING_ID.format(
                                                            edgeGatewayId), pageNo,
                                                        25)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    targetLoadBalancerPoolSummary.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('pool summary result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
            return targetLoadBalancerPoolSummary
        except:
            raise

    @isSessionExpired
    def getVirtualServiceDetails(self, edgeGatewayId):
        """
            Description :   Fetch details of virtual service data of a edge gateway
            Parameters  :   edgeGatewayId - ID of edge gateway whose data is to be fetched
            Returns     :   List of virtual service data of edge gateway(LIST)
        """
        try:
            url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.EDGE_GATEWAY_LOADBALANCER_VIRTUALSERVICE_USING_ID.format(edgeGatewayId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved load balancer virtual service details successfully")
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
            else:
                raise Exception('Failed to fetch load balancer virtual service details')
            pageNo = 1
            pageSizeCount = 0
            targetLoadBalancerVirtualServiceSummary = []
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.EDGE_GATEWAY_LOADBALANCER_VIRTUALSERVICE_USING_ID.format(
                                                            edgeGatewayId), pageNo,
                                                        25)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    targetLoadBalancerVirtualServiceSummary.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('virtual service summary result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    raise Exception('Failed to fetch load balancer virtual service details')
            return targetLoadBalancerVirtualServiceSummary
        except:
            raise

    @isSessionExpired
    def getServiceEngineGroupAssignment(self, edgeGatewayName):
        """
            Description :   Fetch details of service engine groups in a edge gateway
            Parameters  :   edgeGatewayName - Name of edge gateway whose data is to be fetched
            Returns     :   List of service engine groups present in edge gateway(LIST)
        """
        try:
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.ASSIGN_SERVICE_ENGINE_GROUP_URI)
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved service engine group details successfully")
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
            else:
                raise Exception('Failed to fetch service engine group details')
            pageNo = 1
            pageSizeCount = 0
            serviceEngineGroupList = []
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.ASSIGN_SERVICE_ENGINE_GROUP_URI,
                                                        pageNo,
                                                        25)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    serviceEngineGroupList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('virtual service summary result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    raise Exception('Failed to fetch load balancer virtual service details')
            serviceEngineGroupInEdgeGatewayList = list(filter(
                lambda seg: seg['gatewayRef']['name'] == edgeGatewayName, serviceEngineGroupList))
            return serviceEngineGroupInEdgeGatewayList
        except:
            raise

    def getPoolDataUsingPoolId(self, poolId):
        """
           Description :   Fetch data of a load balancer pool using pool id
           Parameters  :   poolId - ID of load balancer pool(STRING)
           Returns     :   data of load balancer pool(DICT)
        """
        try:
            # url to fetch pool data using pool id
            url = '{}{}/{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                   vcdConstants.EDGE_GATEWAY_LOADBALANCER_POOLS,
                                   poolId)
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved pool details for pool id - {} successfully".format(poolId))
                responseDict = response.json()
                return responseDict
            else:
                raise Exception('Failed to fetch service engine group details')
        except:
            raise

    @isSessionExpired
    def loadBalancerRollback(self):
        """
        Description:  Rollback for load balancer service of target edge gateway
        """
        try:
            # Check if services were configured using metadata
            if not isinstance(self.rollback.metadata.get("configureServices", {}).get("configureLoadBalancer"), bool):
                return

            # check api version for load balancer rollback
            if float(self.version) <= float(vcdConstants.API_VERSION_PRE_ZEUS):
                return

            loggingDone = False

            # Iterating over edge gateway id list
            for edgeGateway in self.rollback.apiData['targetEdgeGateway']:
                logger.debug("Removing load balancer configuration from target edge gateway '{}'".format(edgeGateway['name']))
                edgeGatewayId = edgeGateway['id']
                # Fetching virtual service configured on edge gateway
                virtualServices = self.getVirtualServiceDetails(edgeGatewayId)
                # Fetcing load balancer pools configured on target edge gateway
                loadBalancerPools = self.getPoolSumaryDetails(edgeGatewayId)
                # Certificates list used for pools configuration
                certificatesIds = set()

                # Removing virtual services from target edge gateway
                # Iterating over virtual service list to delete them
                for virtualService in virtualServices:
                    if not loggingDone:
                        logger.info("Rollback: Removing load balancer configuration")
                        loggingDone = True
                    virtualServiceId = virtualService['id']
                    # Delete virtual service delete url
                    virtualServiceDeleteUrl = '{}{}/{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                               vcdConstants.EDGE_GATEWAY_LOADBALANCER_VIRTUAL_SERVER,
                                                               virtualServiceId)
                    response = self.restClientObj.delete(virtualServiceDeleteUrl, self.headers)
                    if response.status_code == requests.codes.accepted:
                        # successful deletion of virtual server from edge gateway
                        taskUrl = response.headers['Location']
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug(f'Deleted virtual service {virtualService["name"]} from edge gateway {edgeGateway["name"]}')
                    else:
                        # Error response dict in case of failure
                        errorResponse = response.json()
                        raise Exception(
                            "Failed to delete virtual service {} from edge gateway {} due to error {}".format(
                                virtualService["name"], edgeGateway["name"], errorResponse['message']))

                # Removing load balancer pools from target edge gateway
                # Iterating over load balancer pools list
                for pool in loadBalancerPools:
                    poolId = pool['id']

                    # Retrieving certificates used for pools creation
                    poolData = self.getPoolDataUsingPoolId(poolId)
                    if poolData.get('caCertificateRefs'):
                        for certificate in poolData.get('caCertificateRefs'):
                            certificatesIds.add(certificate['id'])

                    # Delete load balancer pool url
                    loadBalancerPoolDeleteUrl = '{}{}/{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                 vcdConstants.EDGE_GATEWAY_LOADBALANCER_POOLS,
                                                                 poolId)
                    response = self.restClientObj.delete(loadBalancerPoolDeleteUrl, self.headers)
                    if response.status_code == requests.codes.accepted:
                        # successful deletion of virtual server from edge gateway
                        taskUrl = response.headers['Location']
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug(
                            f'Deleted load balancer pool {pool["name"]} from edge gateway {edgeGateway["name"]}')
                    else:
                        # Error response dict in case of failure
                        errorResponse = response.json()
                        raise Exception(
                            "Failed to delete load balancer pool {} from edge gateway {} due to error {}".format(
                                pool["name"], edgeGateway["name"], errorResponse['message']))

                # Removing service engine group from target edge gateway
                # Iterating over service engine groups list
                serviceEngineGroupList = self.getServiceEngineGroupAssignment(edgeGateway['name'])
                for serviceEngineGroup in serviceEngineGroupList:
                    serviceEngineGroupAssignmentId = serviceEngineGroup['id']
                    # Remove service engine group from edge gateway url
                    serviceEngineGroupDeleteUrl = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                   vcdConstants.ASSIGN_SERVICE_ENGINE_GROUP_URI,
                                                                   serviceEngineGroupAssignmentId
                                                                   )
                    response = self.restClientObj.delete(serviceEngineGroupDeleteUrl, self.headers)
                    if response.status_code == requests.codes.accepted:
                        # successful removal of service engine group from edge gateway
                        taskUrl = response.headers['Location']
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug(
                            f'Removed service engine group {serviceEngineGroup["serviceEngineGroupRef"]["name"]} from edge gateway {edgeGateway["name"]}')
                    else:
                        # Error response dict in case of failure
                        errorResponse = response.json()
                        raise Exception(
                            "Failed to remove service engine group {} from edge gateway {} due to error {}".format(
                                serviceEngineGroup["serviceEngineGroupRef"]["name"], edgeGateway["name"], errorResponse['message']))

                # Disabling load balacer service from target edge gateway
                self.enableLoadBalancerService(edgeGatewayId, edgeGateway['name'], rollback=True)

                # Deleting certificates from vCD tenant portal
                # Getting certificates from org vdc tenant portal
                lbCertificates = self.getCerticatesFromTenant()
                # Deleting the certificates used in pool configuration
                for certId in certificatesIds:
                    if certId not in lbCertificates.values():
                        # If certicate not present then continue
                        logger.debug(f'Certificate {certId} already removed')
                    else:
                        # Delete certificate url
                        certDeleteUrl = '{}{}/{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                         'ssl/cetificateLibrary',
                                                         certId)
                        # Adding context header to delete certificate from tenant portal
                        self.headers['X-VMWARE-VCLOUD-TENANT-CONTEXT'] = \
                                        self.rollback.apiData['Organization']['@id'].split(':')[-1]
                        self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                        response = self.restClientObj.delete(certDeleteUrl, self.headers)
                        if response.status_code == requests.codes.no_content:
                            # successful delete of certificate from tenant portal
                            logger.debug(f'Certificate {certId} successfully deleted from tenant portal')
                            # Removing context header after successful deletion of certificate
                            del self.headers['X-VMWARE-VCLOUD-TENANT-CONTEXT']
                        else:
                            # Trying to delete certificate using different url
                            # Delete certificate url
                            certDeleteUrl = '{}{}/{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                             'ssl/certificateLibrary',
                                                             certId)
                            response = self.restClientObj.delete(certDeleteUrl, self.headers)
                            if response.status_code == requests.codes.no_content:
                                # successful delete of certificate from tenant portal
                                logger.debug(f'Certificate {certId} successfully deleted from tenant portal')
                                # Removing context header after successful deletion of certificate
                                del self.headers['X-VMWARE-VCLOUD-TENANT-CONTEXT']
                            else:
                                # Error response dict in case of failure
                                errorResponse = response.json()
                                raise Exception(
                                    "Failed to delete certificate {} from vCD tenant portal due to error {}".format(
                                        certId,
                                        errorResponse['message']))

        except:
            raise

    @description("configuration of LoadBalancer")
    @remediate
    def configureLoadBalancer(self, nsxvObj, ServiceEngineGroupName, loadBalancerVIPSubnet):
        """
        Description :   Configure LoadBalancer service target edge gateway
        Params      :   nsxvObj - NSXVOperations class object
                        ServiceEngineGroupName - Name of service engine group for load balancer configuration (STRING)
                        loadBalancerVIPSubnet - Subnet for loadbalancer virtual service VIP configuration
        """
        try:
            if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                logger.debug('Load Balancer is getting configured')

            for sourceEdgeGateway in self.rollback.apiData['sourceEdgeGateway']:
                sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
                targetEdgeGatewayId = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'],
                                self.rollback.apiData['targetEdgeGateway']))[0]['id']
                targetEdgeGatewayName = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'],
                                self.rollback.apiData['targetEdgeGateway']))[0]['name']

                # url to retrieve the load balancer config info
                url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                      vcdConstants.NETWORK_EDGES,
                                      vcdConstants.EDGE_GATEWAY_LOADBALANCER_CONFIG.format(sourceEdgeGatewayId))
                # get api call to retrieve the load balancer config info
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = xmltodict.parse(response.content)
                    # checking if load balancer is enabled, if so raising exception
                    if responseDict['loadBalancer']['enabled'] == "true":
                        if not float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                            raise Exception("Load Balancer service is configured in the Source edge gateway {} but not supported in the Target".format(sourceEdgeGateway['name']))
                        serviceEngineGroupResultList = self.getServiceEngineGroupDetails()
                        if not serviceEngineGroupResultList:
                             raise Exception('Service Engine Group does not exist.')

                        logger.debug("Configuring LoadBalancer Services in Target Edge Gateway - {}".format(sourceEdgeGateway['name']))
                        serviceEngineGroupDetails = [serviceEngineGroup for serviceEngineGroup in
                                                     serviceEngineGroupResultList if
                                                     serviceEngineGroup['name'] == ServiceEngineGroupName]
                        if not serviceEngineGroupDetails:
                            raise Exception("Service Engine Group {} is not present in Avi.".format(ServiceEngineGroupName))
                        self.serviceEngineGroupName = serviceEngineGroupDetails[0]['name']
                        self.serviceEngineGroupId = serviceEngineGroupDetails[0]['id']
                        # enable load balancer service
                        self.enableLoadBalancerService(targetEdgeGatewayId, targetEdgeGatewayName)
                        # service engine group assignment to target edge gateway
                        self.assignServiceEngineGroup(targetEdgeGatewayId, targetEdgeGatewayName, serviceEngineGroupDetails[0])
                        # creating pools
                        self.createLoadBalancerPools(sourceEdgeGatewayId, targetEdgeGatewayId, targetEdgeGatewayName, nsxvObj)
                        # creating load balancer virtual server
                        self.createLoadBalancerVirtualService(sourceEdgeGatewayId, targetEdgeGatewayId, targetEdgeGatewayName, loadBalancerVIPSubnet)
                    else:
                        logger.debug("LoadBalancer Service is in disabled state in Source Edge Gateway - {}".format(sourceEdgeGateway['name']))
                else:
                    errorResponseData = response.json()
                    raise Exception(
                        "Failed to fetch load balancer config from source edge gateway '{}' due to error {}".format(
                            targetEdgeGatewayName, errorResponseData['message']
                        ))
            logger.info('Target Edge gateway services got configured successfully.')
        except Exception:
            # Updating execution result in metadata in case of failure
            self.rollback.executionResult['configureServices']['configureLoadBalancer'] = False
            self.saveMetadataInOrgVdc()
            raise

    @isSessionExpired
    def createDNATRuleForLoadBalancer(self, edgeGatewayId, ruleName, sourceIP, destinationIp, port):
        """
            Description :   Create DNAT rule for a virtual service of load balancer
            Params      :   edgeGatewayId - ID of target edge gateway (STRING)
                            ruleName - Name of DNAT rule to be created (STRING)
                            sourceIP - VIP used in target edge gateway (STRING)
                            destinationIp - VIP used in source edge gateway (STRING)
                            port - port used for port forwarding (INT)
            """
        try:
            logger.debug(f"Creating DNAT rule {ruleName} for virtual service on edge gateway {edgeGatewayId}")
            # Create NAT rule url
            url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                  vcdConstants.ALL_EDGE_GATEWAYS,
                                  vcdConstants.T1_ROUTER_NAT_CONFIG.format(edgeGatewayId))
            # Payload data for creating DNAT rule
            payloadDict = {
                "ruleId": ruleName,
                "ruleTag": ruleName,
                "ruleDescription": "",
                "enabled": "true",
                "action": "DNAT",
                "loggingEnabled": "true",
                "version": "",
                "originalAddress": destinationIp,
                "translatedAddress": sourceIP,
                "dnatExternalPort": port
            }

            # Filepath of template json file
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')

            # Creating payload for DNAT rule creation
            payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CREATE_DNAT_TEMPLATE, apiVersion=self.version)
            payloadData = json.loads(payloadData)

            # Deleting version key from payload data as it is not required
            del payloadData['version']

            # Getting the application port profile list
            applicationPortProfilesList = self.getApplicationPortProfiles()
            # Fetching name and id of specific application port profile for corresponding port
            protocol_port_name, protocol_port_id = self._searchApplicationPortProfile(
                applicationPortProfilesList, 'tcp', port)
            payloadData["applicationPortProfile"] = {"name": protocol_port_name, "id": protocol_port_id}

            # From VCD v10.2.2, firewallMatch to external address to be provided for DNAT rules
            if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS_10_2_2):
                payloadData["firewallMatch"] = "MATCH_EXTERNAL_ADDRESS"

            # Create rule api call
            self.createNatRuleTask(payloadData, url)

        except:
            raise

    def createLoadBalancerVirtualService(self, sourceEdgeGatewayId, targetEdgeGatewayId, targetEdgeGatewayName, loadBalancerVIPSubnet):
        """
            Description :   Configure LoadBalancer virtual service on target edge gateway
            Params      :   sourceEdgeGatewayId - ID of source edge gateway (STRING)
                            targetEdgeGatewayId - ID of target edge gateway (STRING)
                            targetEdgeGatewayName - Name of target edge gateway (STRING)
                            loadBalancerVIPSubnet - Subnet for loadbalancer virtual service VIP configuration (STRING)
        """
        try:
            # Fetching virtual service configured on edge gateway
            virtualServices = self.getVirtualServiceDetails(targetEdgeGatewayId)

            poolNameIdDict = {}
            # url for getting edge gateway load balancer virtual servers configuration
            url = '{}{}'.format(
                vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                vcdConstants.EDGE_GATEWAY_VIRTUAL_SERVER_CONFIG.format(sourceEdgeGatewayId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                virtualServersData = xmltodict.parse(response.content)
            else:
                raise Exception('Failed to get source edge gateway load balancer virtual servers configuration')

            # getting loadbalancer config
            url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                  vcdConstants.NETWORK_EDGES,
                                  vcdConstants.EDGE_GATEWAY_LOADBALANCER_CONFIG.format(sourceEdgeGatewayId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = xmltodict.parse(response.content)
                # Fetching pools data from response
                sourceLBPools = responseDict['loadBalancer'].get('pool') \
                    if isinstance(responseDict['loadBalancer'].get('pool'), list) \
                    else [responseDict['loadBalancer'].get('pool')]

                # Fetching application profiles data from response
                applicationProfiles = responseDict['loadBalancer'].get('applicationProfile') \
                    if isinstance(responseDict['loadBalancer'].get('applicationProfile'), list) \
                    else [responseDict['loadBalancer'].get('applicationProfile')]

            else:
                raise Exception('Failed to get load balancer configuration from source edge gateway - {}'.format(
                    targetEdgeGatewayName))

            # Fetching load balancer pools data
            targetLoadBalancerPoolSummary = self.getPoolSumaryDetails(targetEdgeGatewayId)

            # Iterating over pools to find the pool to be used in virtual service
            for pool in sourceLBPools:
                poolNameIdDict[pool['poolId']] = pool['name'], [targetPool for targetPool in
                                                                targetLoadBalancerPoolSummary
                                                                if targetPool['name'] == pool['name']
                                                                ][0]['id']

            virtualServersData = virtualServersData['loadBalancer']['virtualServer'] if isinstance(
                virtualServersData['loadBalancer']['virtualServer'], list) else \
                [virtualServersData['loadBalancer']['virtualServer']]

            # if subnet is not provided in user input use default subnet
            loadBalancerVIPSubnet = loadBalancerVIPSubnet if loadBalancerVIPSubnet else '192.168.255.128/28'

            # Creating a list of hosts in a subnet
            hostsListInSubnet = list(ipaddress.ip_network(loadBalancerVIPSubnet, strict=False).hosts())

            if len(hostsListInSubnet) < len(virtualServersData):
                raise Exception("Number of hosts in network - {} if less than the number of virtual server in edge gateway{}".format(loadBalancerVIPSubnet, targetEdgeGatewayName))

            for virtualServer in virtualServersData:
                # IP address to be used for VIP in virtual service
                virtualIpAddress = hostsListInSubnet.pop(0)

                # If virtual service is already created on target then skip it
                if virtualServer['name'] in [service['name'] for service in virtualServices]:
                    logger.debug(f'Virtual service {virtualServer["name"]} already created on target edge gateway {targetEdgeGatewayName}')
                    # Incrementing the IP address for next virtual service
                    hostsListInSubnet.pop(0)
                    continue
                payloadDict = {
                    'virtualServiceName': virtualServer['name'],
                    'description': virtualServer.get('description', ''),
                    'enabled': virtualServer['enabled'],
                    'ipAddress': str(virtualIpAddress),
                    'poolName': poolNameIdDict[virtualServer['defaultPoolId']][0],
                    'poolId': poolNameIdDict[virtualServer['defaultPoolId']][1],
                    'gatewayName': targetEdgeGatewayName,
                    'gatewayId': targetEdgeGatewayId,
                    'serviceEngineGroupName': self.serviceEngineGroupName,
                    'serviceEngineGroupId': self.serviceEngineGroupId,
                    'port': virtualServer['port'],
                    'sslEnabled': True if virtualServer['protocol'] == 'https' else False
                }

                filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
                payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                          componentName=vcdConstants.COMPONENT_NAME,
                                                          templateName=vcdConstants.CREATE_LOADBALANCER_VIRTUAL_SERVICE)
                payloadData = json.loads(payloadData)

                certificateForTCP = False

                if virtualServer['protocol'] == 'https' or virtualServer['protocol'] == 'tcp':
                    applicationProfileId = virtualServer['applicationProfileId']
                    applicationProfileData = [profile for profile in applicationProfiles
                                              if profile['applicationProfileId'] == applicationProfileId]

                    applicationProfileData = applicationProfileData[0] if applicationProfileData else None

                    if applicationProfileData:
                        certificateObjectId = applicationProfileData.get('clientSsl', {}).get('serviceCertificate', None)

                        if virtualServer['protocol'] == 'tcp' and certificateObjectId:
                            certificateForTCP = True

                        if certificateObjectId:
                            # Getting certificates from org vdc tenant portal
                            lbCertificates = self.getCerticatesFromTenant()

                            # Certificates payload
                            certificatePayload = {
                                'name': certificateObjectId,
                                'id': lbCertificates[certificateObjectId]
                                 }
                            payloadData["certificateRef"] = certificatePayload
                else:
                    payloadData["certificateRef"] = None

                applicationProfilePayload = {
                    "type": "HTTP" if virtualServer['protocol'] == 'http'
                    else "HTTPS" if virtualServer['protocol'] == 'https'
                    else "L4 TLS" if certificateForTCP
                    else "L4",
                    "systemDefined": True
                }
                payloadData['applicationProfile'] = applicationProfilePayload

                payloadData = json.dumps(payloadData)
                url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.EDGE_GATEWAY_LOADBALANCER_VIRTUAL_SERVER)
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                # post api call to configure virtual server for load balancer service
                response = self.restClientObj.post(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Successfully created virtual server - {} for load balancer on target edge gateway'.format(
                        virtualServer['name']
                    ))

                    # Name of DNAT rule to be created for load balancer virtual service
                    DNATRuleName = f'{virtualServer["name"]}-DNAT-RULE'
                    # Creating DNAT rule for virtual service
                    self.createDNATRuleForLoadBalancer(targetEdgeGatewayId, DNATRuleName, str(virtualIpAddress),
                                                       virtualServer['ipAddress'], virtualServer['port'])
                else:
                    errorResponseData = response.json()
                    raise Exception(
                        "Failed to create virtual server '{}' for load balancer on target edge gateway '{}' due to error {}".format(
                            virtualServer['name'], targetEdgeGatewayName, errorResponseData['message']
                        ))
        except:
            raise

    @isSessionExpired
    def createLoadBalancerPools(self, sourceEdgeGatewayId, targetEdgeGatewayId, targetEdgeGatewayName, nsxvObj):
        """
            Description :   Configure LoadBalancer service pools on target edge gateway
            Params      :   sourceEdgeGatewayId - ID of source edge gateway (STRING)
                            targetEdgeGatewayId - ID of target edge gateway (STRING)
                            targetEdgeGatewayName - Name of target edge gateway (STRING)
                            nsxvObj - NSXVOperations class object (OBJECT)
        """
        # Fetching load balancer pools configured on target edge gateway
        loadBalancerPools = self.getPoolSumaryDetails(targetEdgeGatewayId)
        # getting loadbalancer config
        url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                              vcdConstants.NETWORK_EDGES,
                              vcdConstants.EDGE_GATEWAY_LOADBALANCER_CONFIG.format(sourceEdgeGatewayId))
        response = self.restClientObj.get(url, self.headers)
        if response.status_code == requests.codes.ok:
            responseDict = xmltodict.parse(response.content)
            # Fetching pools data from response
            pools = responseDict['loadBalancer'].get('pool') \
                if isinstance(responseDict['loadBalancer'].get('pool'), list) \
                else [responseDict['loadBalancer'].get('pool')]

            # Fetching health monitors data from response
            healthMonitors = responseDict['loadBalancer'].get('monitor') \
                if isinstance(responseDict['loadBalancer'].get('monitor'), list) \
                else [responseDict['loadBalancer'].get('monitor')]

            # Fetching application profiles data from response
            applicationProfiles = responseDict['loadBalancer'].get('applicationProfile') \
                if isinstance(responseDict['loadBalancer'].get('applicationProfile'), list) \
                else [responseDict['loadBalancer'].get('applicationProfile')]

            # url for getting edge gateway load balancer virtual servers configuration
            url = '{}{}'.format(
                vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                vcdConstants.EDGE_GATEWAY_VIRTUAL_SERVER_CONFIG.format(sourceEdgeGatewayId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                virtualServersData = xmltodict.parse(response.content)
            else:
                raise Exception('Failed to get source edge gateway load balancer virtual servers configuration')

            virtualServersData = virtualServersData['loadBalancer']['virtualServer'] if isinstance(
                virtualServersData['loadBalancer']['virtualServer'], list) else \
                [virtualServersData['loadBalancer']['virtualServer']]

            # Fetching object id's of certificates used for https configuration
            objectIdsOfCertificates = [profile['clientSsl']['serviceCertificate'] for profile in applicationProfiles if profile.get('clientSsl')]

            # Getting certificates from org vdc tenant portal
            lbCertificates = self.getCerticatesFromTenant()

            # Uploading certificate to org vdc tenant portal
            for objectId in objectIdsOfCertificates:
                if objectId in lbCertificates:
                    logger.debug('Certificate {} already present in vCD tenant portal'.format(objectId))
                    continue
                else:
                    # Fetch certificate from nsx-v
                    certificate = nsxvObj.certRetrieval(objectId)
                    logger.debug('Uploading the certificate {} for load balancer HTTPS configuration'.format(objectId))
                    self.uploadCertificate(certificate, objectId)

            # Iterating over pools to create pools for load balancer in target
            for poolData in pools:

                persistenceProfile = {}

                # finding virtual service corresponding to this load balancer pool
                virtualServer = list(filter(lambda vserver: vserver['defaultPoolId'] == poolData['poolId'], virtualServersData))

                if virtualServer:
                    applicationProfileId = virtualServer[0]['applicationProfileId']
                    applicationProfileData = list(filter(lambda profile: profile['applicationProfileId'] == applicationProfileId, applicationProfiles))[0]

                    # creating persistence profile payload for pool creation
                    if applicationProfileData and applicationProfileData.get('persistence'):
                        persistenceData = applicationProfileData.get('persistence')
                        if persistenceData['method'] == 'cookie':
                            persistenceProfile = {
                                "type": "HTTP_COOKIE",
                                "value": persistenceData['cookieName']
                            }
                        if persistenceData['method'] == 'sourceip':
                            persistenceProfile = {
                                "type": "CLIENT_IP",
                                "value": ""}

                # If pool is already created on target then skip
                if poolData['name'] in [pool['name'] for pool in loadBalancerPools]:
                    logger.debug(f'Pool {poolData["name"]} already created on target edge gateway {targetEdgeGatewayName}')
                    continue

                # Filtering the health monitors used in pools
                healthMonitorUsedInPool = list(filter(
                    lambda montitor:montitor['monitorId'] == poolData.get('monitorId', None), healthMonitors))

                # Fetching pool memmers from pool data
                if poolData.get('member'):
                    poolMembers = poolData.get('member') if isinstance(poolData.get('member'), list) else [poolData.get('member')]
                else:
                    poolMembers = []

                # file path of template.json
                filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')

                # Creating pool dict for load balancer pool creation
                payloadDict = {
                    'poolName': poolData['name'],
                    'description': poolData.get('description', ''),
                    'algorithm': "LEAST_CONNECTIONS" if poolData['algorithm'] == 'leastconn'
                                                     else poolData['algorithm'].upper().replace('-', '_'),
                    'gatewayName': targetEdgeGatewayName,
                    'gatewayId': targetEdgeGatewayId
                }
                payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                          componentName=vcdConstants.COMPONENT_NAME,
                                                          templateName=vcdConstants.CREATE_LOADBALANCER_POOL)
                payloadData = json.loads(payloadData)

                # Adding pool members in pool payload
                targetPoolMembers = []
                for member in poolMembers:
                    memberDict = {
                                "ipAddress": member['ipAddress'],
                                "port": member['port'],
                                "ratio": member['weight'],
                                "enabled": True if member['condition'] == 'enabled' else False
                                }
                    targetPoolMembers.append(memberDict)
                payloadData['members'] = targetPoolMembers

                # adding persistence profile in payload
                if persistenceProfile:
                    payloadData['persistenceProfile'] = persistenceProfile

                # Adding health monitors in pool payload
                if healthMonitorUsedInPool:
                    healthMonitorsForPayload = [
                        {
                            "type": 'HTTP' if healthMonitorUsedInPool[0]['type'] == 'http'
                                          else 'HTTPS' if healthMonitorUsedInPool[0]['type'] == 'https'
                                          else 'TCP' if healthMonitorUsedInPool[0]['type'] == 'tcp'
                                          else 'UDP' if healthMonitorUsedInPool[0]['type'] == 'udp'
                                          else 'PING' if healthMonitorUsedInPool[0]['type'] == 'icmp'
                                          else None,
                            "systemDefined": True
                        }
                    ]

                    payloadData['healthMonitors'] = healthMonitorsForPayload

                # Adding certificates in pool payload if https config is present
                if healthMonitorUsedInPool and healthMonitorUsedInPool[0]['type'] == 'https':
                    lbCertificates = self.getCerticatesFromTenant()
                    certificatePayload = [{'name': objectId, 'id': lbCertificates[objectId]}for objectId in objectIdsOfCertificates]
                    payloadData["caCertificateRefs"] = certificatePayload
                else:
                    payloadData["caCertificateRefs"] = None

                payloadData = json.dumps(payloadData)

                # URL to create load balancer pools
                url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.EDGE_GATEWAY_LOADBALANCER_POOLS)
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                # post api call to configure pool for load balancer service
                response = self.restClientObj.post(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                else:
                    # Error response JSON in case load balancer pool creation fails
                    errorResponseData = response.json()
                    raise Exception("Failed to create pool for load balancer on target edge gateway '{}' due to error {}".format(
                        targetEdgeGatewayName, errorResponseData['message']
                    ))
        else:
            raise Exception('Failed to get load balancer configuration from source edge gateway - {}'.format(targetEdgeGatewayName))

    @isSessionExpired
    def enableLoadBalancerService(self, targetEdgeGatewayId, targetEdgeGatewayName, rollback=False):
        """
            Description :   Enabling LoadBalancer virtual service on target edge gateway
            Params      :   targetEdgeGatewayId - ID of target edge gateway (STRING)
                            targetEdgeGatewayName - Name of target edge gateway (STRING)
                            rollback - flag to decide whether to disable or enable load balancer(BOOL)
        """
        try:
            if rollback:
                logger.debug('Disabling LoadBalancer service on target Edge Gateway-{} as a part of rollback'.format(
                    targetEdgeGatewayName))
                payloadDict = {"enabled": False}
            else:
                logger.debug('Enabling LoadBalancer service on target Edge Gateway-{}'.format(targetEdgeGatewayName))
                payloadDict = {"enabled": True}

            # url to enable loadbalancer service on target edge gateway
            url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                  vcdConstants.ALL_EDGE_GATEWAYS,
                                  vcdConstants.LOADBALANCER_ENABLE_URI.format(targetEdgeGatewayId))
            payloadData = json.dumps(payloadDict)
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE

            # get api call to fetch load balancer service status from edge gateaway
            response = self.restClientObj.get(url, self.headers, data=payloadData)
            if response.status_code == requests.codes.ok:
                responseData = response.json()
            else:
                errorResponseData = response.json()
                raise Exception('Failed to fetch LoadBalancer service data from target Edge Gateway-{} with error-{}'.format(
                    targetEdgeGatewayName, errorResponseData['message']))

            lbServiceStatus = responseData['enabled']
            if rollback and not lbServiceStatus:
                logger.debug('Load Balancer already disabled on target Edge Gateway-{}'.format(targetEdgeGatewayName))
                return

            if not rollback and lbServiceStatus:
                logger.debug('Load Balancer already enabled on target Edge Gateway-{}'.format(targetEdgeGatewayName))
                return

                # put api call to enable load balancer on target edge gateway
            response = self.restClientObj.put(url, self.headers, data=payloadData)
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl)
                if rollback:
                    logger.debug('Successfully disabled LoadBalancer service on target Edge Gateway-{}'.format(
                        targetEdgeGatewayName))
                else:
                    logger.debug('Successfully enabled LoadBalancer service on target Edge Gateway-{}'.format(targetEdgeGatewayName))
            else:
                errorResponseData = response.json()
                if rollback:
                    raise Exception(
                        'Failed to disable LoadBalancer service on target Edge Gateway-{} with error-{}'.format(
                            targetEdgeGatewayName, errorResponseData['message']))
                else:
                    raise Exception('Failed to enable LoadBalancer service on target Edge Gateway-{} with error-{}'.format(targetEdgeGatewayName, errorResponseData['message']))
        except Exception:
            raise

    @isSessionExpired
    def assignServiceEngineGroup(self, targetEdgeGatewayId, targetEdgeGatewayName, serviceEngineGroupDetails):
        """
            Description :   Assign Service Engine Group on target Edge Gateway-
            Params      :   targetEdgeGatewayId - ID of target edge gateway (STRING)
                            targetEdgeGatewayName - Name of target edge gateway (STRING)
                            serviceEngineGroupDetails - Details of service engine group (DICT)
        """
        try:
            logger.debug('Assigning Service Engine Group on target Edge Gateway-{}'.format(targetEdgeGatewayName))

            # Fetching list of service engine groups added in edge gateway
            serviceEngineGroupList = self.getServiceEngineGroupAssignment(targetEdgeGatewayName)
            for seg in serviceEngineGroupList:
                if seg['serviceEngineGroupRef']['name'] == self.serviceEngineGroupName:
                    logger.debug('Already assigned Service Engine Group on target Edge Gateway-{}'.format(
                        targetEdgeGatewayName))
                    return

            # url to assign avi service engine group on target edge gateway
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                  vcdConstants.ASSIGN_SERVICE_ENGINE_GROUP_URI)
            payloadDict = {
                            "minVirtualServices": 0,
                            "maxVirtualServices": serviceEngineGroupDetails['maxVirtualServices'],
                            "serviceEngineGroupRef": {
                                "name": self.serviceEngineGroupName,
                                "id": self.serviceEngineGroupId
                            },
                            "gatewayRef": {
                                "name": targetEdgeGatewayName,
                                "id": targetEdgeGatewayId
                            }
                        }
            payloadData = json.dumps(payloadDict)
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            # post api call to configure ipsec rules on target edge gateway
            response = self.restClientObj.post(url, self.headers, data=payloadData)
            if response.status_code == requests.codes.accepted:
                # if successful configuration of ipsec rules
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl)
                logger.debug('Successfully assigned Service Engine Group on target Edge Gateway-{}'.format(targetEdgeGatewayName))
            else:
                errorResponseData = response.json()
                raise Exception("Failed to assign Service Engine Group on target Edge Gateway-{} with error-{}".format(targetEdgeGatewayName, errorResponseData['message']))
        except:
            raise

    @isSessionExpired
    def getDcGroupNetworks(self, dcGroupId):
        """
        Description :   Fetch all networks from the provided DC group
        Parameters  :   dcGroupId - Id of the DC Group (STR)
        Returns     :   resultFetched - List of networks associated with DC Group (LIST)
        """
        try:
            pageSize = vcdConstants.DEFAULT_QUERY_PAGE_SIZE
            base_url = "{}{}".format(
                vcdConstants.OPEN_API_URL.format(self.ipAddress),
                vcdConstants.ALL_ORG_VDC_NETWORKS)
            query = f'&filterEncoded=true&filter=((ownerRef.id=={dcGroupId});(crossVdcNetworkId==null))'

            # Get first page of query
            pageNo = 1
            url = f"{base_url}?page={pageNo}&pageSize={pageSize}{query}"
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE

            response = self.restClientObj.get(url, self.headers)
            if not response.status_code == requests.codes.ok:
                raise Exception(
                    f'Error occurred while retrieving DC group networks: {response.json()["message"]}')

            # Store first page result and preapre for second page
            responseContent = response.json()
            resultTotal = responseContent['resultTotal']
            resultFetched = responseContent['values']
            pageNo += 1

            # Return if results are empty
            if resultTotal == 0:
                return []

            # Query second page onwards until resultTotal is reached
            while len(resultFetched) < resultTotal:
                url = f"{base_url}?page={pageNo}&pageSize={pageSize}{query}"
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                response = self.restClientObj.get(url, self.headers)
                if not response.status_code == requests.codes.ok:
                    raise Exception(
                        f'Error occurred while retrieving DC group networks: {response.json()["message"]}')

                responseContent = response.json()
                resultFetched.extend(responseContent['values'])
                resultTotal = responseContent['resultTotal']
                logger.debug(f'DC group networks result pageSize = {len(resultFetched)}')
                pageNo += 1

            logger.debug(f'Total DC group networks count = {len(resultFetched)}')
            logger.debug(f'DC group networks successfully retrieved')

            return resultFetched

        except Exception as e:
            logger.error(f'Error occurred while retrieving Named Disks: {e}')
            raise

    def getTargetEntitiesToDcGroupMap(self):
        """
        Description :   Map entities like Org VDC and Org VDC network to DC groups which they are part of.
        Returns     :   dict with with key as entity ID(Org VDC/Network) and value as set of DC
                        group IDs associated with it (DICT)
        """
        dcGroupsIds = self.rollback.apiData.get('OrgVDCGroupID')
        logger.debug('dcGroupsIds {}'.format(dcGroupsIds))
        dcGroups = self.getOrgVDCGroup()
        dcGroups = {dcGroup['id']: dcGroup for dcGroup in dcGroups}

        targetEntitiesToDcGroupMap = defaultdict(set)
        for dcGroupId in dcGroupsIds.values():
            # Collect Org VDC and all DC Groups associated with it.
            for vdc in dcGroups.get(dcGroupId, {}).get('participatingOrgVdcs', []):
                targetEntitiesToDcGroupMap[vdc['vdcRef']['id']].add(dcGroupId)

            # Create Org VDC network to DC group dictionary. One network should
            # belong to only one Network
            dcGroupNetworks = self.getDcGroupNetworks(dcGroupId)
            for network in dcGroupNetworks:
                targetEntitiesToDcGroupMap[network['id']] = {dcGroupId}

        logger.debug(f'targetEntitiesToDcGroupMap {targetEntitiesToDcGroupMap}')
        return {key: frozenset(value) for key, value in targetEntitiesToDcGroupMap.items()}

    def getDfwRuleScope(self, l3rule, targetEntitiesToDcGroupMap, sourceToTargetOrgNetIds, sourceDfwSecurityGroups):
        """
        Description :   Provides a set of DC groups on which DFW rule is to be created
                        based upon Org VDC networks and AppliedTO section in rule
        Parameters  :   l3rule - DFW rule from source Org VDC (DICT)
                        targetEntitiesToDcGroupMap : Map of entities like Org VDC and Org VDC network to DC groups
                            which they are part of (DICT)
        Returns     :   Set of DC group IDs (SET)
        """
        def all_networks(entities):
            """Returns list of network objects used in entites. If any other object is present in entities return empty list """
            entities = entities if isinstance(entities, list) else [entities]
            networks = set()
            for entity in entities:
                if entity['type'] == 'Network':
                    networks.add(entity['value'])

                elif entity['type'] == 'SecurityGroup':
                    sourceGroup = sourceDfwSecurityGroups[entity['value']]
                    if sourceGroup.get('dynamicMemberDefinition'):
                        return list()

                    if not sourceGroup.get('member'):
                        continue

                    includeMembers = (sourceGroup['member'] if isinstance(sourceGroup['member'], list) else [sourceGroup['member']])
                    for member in includeMembers:
                        if member['type']['typeName'] == 'Network':
                            networks.add(member['objectId'])
                        else:
                            return list()

                else:
                    return list()

            return networks

        data = self.rollback.apiData

        # Case 1
        # If rule has only Org VDC networks(directly or in Security groups),
        # scope of rule will be only DC group to which that network is attached.
        # For this network only rule, only one DC group should match. "appliedToList" parameter from rule is ignored.
        sourceNetworks = list()
        if l3rule.get('sources', {}).get('source'):
             sourceNetworks.extend(all_networks(l3rule['sources']['source']))

        if l3rule.get('destinations', {}).get('destination'):
            sourceNetworks.extend(all_networks(l3rule['destinations']['destination']))

        targetAppliedToScope = set()
        for net in sourceNetworks:
            targetNetworkId = sourceToTargetOrgNetIds[net]
            targetAppliedToScope.update(targetEntitiesToDcGroupMap[targetNetworkId])
        logger.debug(f'new scope {targetAppliedToScope}')

        if targetAppliedToScope:
            if len(targetAppliedToScope) == 1:
                return targetAppliedToScope
            else:
                raise Exception('Invalid DFW rule {}: Network objects in rule belongs to different DC groups'.format(
                    l3rule.get('name')))

        # Case 2
        # If network object is clubbed with other firewall objects check for "appliedToList" scope as per below criteria
        # 1. NSX-V rules with an Org VDC scope will be migrated to all target DC Groups that contain this Org VDC.
        # 2. NSX-V rules with Org VDC network scope will be migrated only to DC Group that contains this network.
        if not l3rule.get('appliedToList'):
            raise Exception('Invalid "appliedTo" for DFW rule {}'.format(l3rule.get('name')))

        sourceAppliedToScope = (
            l3rule['appliedToList']['appliedTo']
            if isinstance(l3rule['appliedToList']['appliedTo'], list)
            else [l3rule['appliedToList']['appliedTo']]
        )

        targetAppliedToScope = set()
        for item in sourceAppliedToScope:
            # NSX-V rules with an Org VDC scope will be migrated to all target DC Groups that contain this Org VDC.
            if item['type'] == 'VDC':
                if not item['value'] == data['sourceOrgVDC']['@id'].split(':')[-1]:
                    raise Exception('Invalid "appliedTo" for DFW rule {}'.format(l3rule.get('name')))
                targetAppliedToScope.update(targetEntitiesToDcGroupMap[data['targetOrgVDC']['@id']])

            # NSX-V rules with Org VDC network scope will be migrated only to DC Group that contains this network.
            elif item['type'] == 'Network':
                targetNetworkId = sourceToTargetOrgNetIds[item['value']]
                if not targetNetworkId:
                    raise Exception('Invalid "appliedTo" for DFW rule {}'.format(l3rule.get('name')))
                targetAppliedToScope.update(targetEntitiesToDcGroupMap[targetNetworkId])

        return targetAppliedToScope

    @description('Configuring source DFW rules in Target VDC groups')
    @remediate
    def configureDFW(self, vcdObjList, sourceOrgVDCId):
        """
        Description :   Configuring source DFW rules in Target VDC groups
        Parameters  :   vcdObjList - List of objects of vcd operations class (LIST)
                        sourceOrgVDCId - ID of source orgVDC(NSX ID format not URN) (STR)
        """
        try:
            if not self.rollback.apiData.get('OrgVDCGroupID'):
                return

            # Acquire lock as dc groups can be common and one thread should configure dfw at a time
            self.lock.acquire(blocking=True)
            logger.info("Configuring DFW Services in VDC groups")

            # getting all the L3 DFW rules
            allLayer3Rules = self.getDistributedFirewallRules(sourceOrgVDCId, ruleType='non-default')
            if not allLayer3Rules:
                logger.debug('DFW rules are not configured')
                return
            allLayer3Rules = allLayer3Rules if isinstance(allLayer3Rules, list) else [allLayer3Rules]

            # sourceToTargetOrgNetIds and targetEntitiesToDcGroupMap is required to identify scope of rule
            sourceToTargetOrgNetIds = {
                vcdObj.rollback.apiData["sourceOrgVDCNetworks"][
                    targetNet[:-4] if targetNet.endswith('-v2t') else targetNet]['id']: targetNetMetadata['id']
                for vcdObj in vcdObjList
                for targetNet, targetNetMetadata in vcdObj.rollback.apiData["targetOrgVDCNetworks"].items()
            }
            targetEntitiesToDcGroupMap = self.getTargetEntitiesToDcGroupMap()

            logger.debug(f'sourceToTargetOrgNetIds {sourceToTargetOrgNetIds}')

            # Collect pre-configured DFW objects.
            applicationPortProfilesList = self.getApplicationPortProfiles()
            sourceDfwSecurityGroups = self.getSourceDfwSecurityGroups()
            allFirewallGroups = self.fetchFirewallGroupsByDCGroup()
            self.getTargetSecurityTags()
            dfwURLs = self.getDfwUrls()

            self.rollback.executionResult["configureTargetVDC"]["configureDFW"] = False
            self.saveMetadataInOrgVdc()

            for l3rule in allLayer3Rules:
                logger.debug('RULE_NAME_{}'.format(l3rule['name']))
                if self.rollback.apiData.get(l3rule['@id']):
                    continue

                # Rule will be applied to all DC groups identified by ruleScopedDcGroups
                ruleScopedDcGroups = self.getDfwRuleScope(
                    l3rule, targetEntitiesToDcGroupMap, sourceToTargetOrgNetIds, sourceDfwSecurityGroups)

                logger.debug(ruleScopedDcGroups)

                # Configure firewall groups for source/destination objects for each DC group identified
                # by ruleScopedDcGroups
                sourceFirewallGroupObjects = destFirewallGroupObjects = dict()
                if l3rule.get('sources', {}).get('source'):
                    sources = (
                        l3rule['sources']['source']
                        if isinstance(l3rule['sources']['source'], list)
                        else [l3rule['sources']['source']])
                    sourceFirewallGroupObjects = self.configureDFWgroups(
                        sources, l3rule['@id'], allFirewallGroups, ruleScopedDcGroups, sourceToTargetOrgNetIds,
                        targetEntitiesToDcGroupMap, sourceDfwSecurityGroups, source=True)
                if l3rule.get('destinations', {}).get('destination'):
                    destinations = (
                        l3rule['destinations']['destination']
                        if isinstance(l3rule['destinations']['destination'], list)
                        else [l3rule['destinations']['destination']])
                    destFirewallGroupObjects = self.configureDFWgroups(
                        destinations, l3rule['@id'], allFirewallGroups, ruleScopedDcGroups, sourceToTargetOrgNetIds,
                        targetEntitiesToDcGroupMap, sourceDfwSecurityGroups, source=False)

                # Preparing payload with parameters which will be common in all DC groups
                # source firewall groups, destination firewall groups will added as per scope of rule
                payloadDict = {
                    'name':
                        f"{l3rule['name']}-{l3rule['@id']}"
                        if l3rule['name'] != 'Default Allow Rule'
                        else 'Default',
                    'enabled': True if l3rule['@disabled'] == 'false' else 'false',
                    'action': 'ALLOW' if l3rule['action'] == 'allow' else 'DROP',
                    'logging': 'true' if l3rule['@logged'] == 'true' else 'false',
                    'ipProtocol':
                        'IPV4' if l3rule['packetType'] == 'ipv4'
                        else 'IPV6' if l3rule['packetType'] == 'ipv6'
                        else 'IPV4_IPV6',
                    'direction':
                        'OUT' if l3rule['direction'] == 'out'
                        else 'IN' if l3rule['direction'] == 'in'
                        else 'IN_OUT',
                }

                # updating the payload with application port profiles
                # checking for the application key in firewallRule
                applicationServicesList = list()
                networkContextProfilesList = list()
                if l3rule.get('services'):
                    if l3rule['services'].get('service'):
                        layer3AppServices = self.getApplicationServicesDetails(sourceOrgVDCId)
                        allNetworkContextProfilesList = self.getNetworkContextProfiles()
                        # list instance of application services
                        firewallRules = l3rule['services']['service'] if isinstance(l3rule['services']['service'], list) else [l3rule['services']['service']]
                        # iterating over the application services
                        for applicationService in firewallRules:
                            for service in layer3AppServices:
                                if applicationService.get('protocolName'):
                                    if applicationService['protocolName'] == 'TCP' or applicationService['protocolName'] == 'UDP':
                                        protocol_name, port_id = self._searchApplicationPortProfile(
                                            applicationPortProfilesList, applicationService['protocolName'],
                                            applicationService['destinationPort'])
                                        applicationServicesList.append({'name': protocol_name, 'id': port_id})
                                    else:
                                        # iterating over the application port profiles
                                        for value in applicationPortProfilesList:
                                            if value['name'] == vcdConstants.IPV6ICMP:
                                                protocol_name, port_id = value['name'], value['id']
                                                applicationServicesList.append(
                                                    {'name': protocol_name, 'id': port_id})
                                    break
                                if service['objectId'] == applicationService['value']:
                                    if service['layer'] == 'layer3' or service['layer'] == 'layer4':
                                        if applicationService['name'] != 'FTP' and applicationService['name'] != 'TFTP':
                                            if service['element']['applicationProtocol'] == 'TCP' or \
                                                    service['element']['applicationProtocol'] == 'UDP':
                                                protocol_name, port_id = self._searchApplicationPortProfile(applicationPortProfilesList, service['element']['applicationProtocol'], service['element']['value'])
                                                applicationServicesList.append({'name': protocol_name, 'id': port_id})
                                        else:
                                            # protocol_name, port_id = [[values['name'], values['id']] for values in applicationPortProfilesList if values['name'] == 'FTP' or values['name'] == 'TFTP']
                                            for values in applicationPortProfilesList:
                                                if values['name'] == applicationService['name']:
                                                    applicationServicesList.append({'name': values['name'], 'id': values['id']})
                                        # if protocol is IPV6ICMP
                                        if service['element']['applicationProtocol'] == 'IPV6ICMP':
                                            # iterating over the application port profiles
                                            for value in applicationPortProfilesList:
                                                if value['name'] == vcdConstants.IPV6ICMP:
                                                    protocol_name, port_id = value['name'], value['id']
                                                    applicationServicesList.append({'name': protocol_name, 'id': port_id})
                                        # if protocal is IPV4ICMP
                                        if service['element']['applicationProtocol'] == 'ICMP':
                                            for value in applicationPortProfilesList:
                                                if value['name'] == service['name'] or value['name'] == service['name'] + ' Request':
                                                    applicationServicesList.append({'name': value['name'], 'id': value['id']})
                                    if service['layer'] == 'layer7':
                                        for contextProfile in allNetworkContextProfilesList.values():
                                            if service['element']['appGuidName'] == contextProfile['name'] or applicationService['name'] == contextProfile['name']:
                                                networkContextProfilesList.append({'name': contextProfile['name'], 'id': contextProfile['id']})
                        payloadDict['applicationPortProfiles'] = applicationServicesList
                        payloadDict['networkContextProfiles'] = networkContextProfilesList
                else:
                    payloadDict['applicationPortProfiles'] = applicationServicesList
                    payloadDict['networkContextProfiles'] = networkContextProfilesList

                # Create Rule
                for dcGroupId in ruleScopedDcGroups:
                    if len(networkContextProfilesList) > 1 and len(applicationServicesList) >= 1:
                        self.thread.spawnThread(
                            self.putDfwMultipleL7Rules, networkContextProfilesList, dfwURLs[dcGroupId], payloadDict,
                            l3rule['name'], dcGroupId, sourceFirewallGroupObjects, destFirewallGroupObjects)
                    else:
                        self.thread.spawnThread(
                            self.putDfwPolicyRules, dfwURLs[dcGroupId], payloadDict, l3rule['name'], dcGroupId,
                            sourceFirewallGroupObjects, destFirewallGroupObjects)

                # Halting the main thread till all the threads have completed their execution
                self.thread.joinThreads()
                if self.thread.stop():
                    raise Exception('Failed to create distributed firewall rule')
                self.rollback.apiData[l3rule['@id']] = True

        except DfwRulesAbsentError as e:
            logger.debug(e)

        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def getDfwUrls(self):
        """
        Description :   Get DFW policy and form URL
        """
        dfwURLs = dict()
        for dcGroupId in self.rollback.apiData.get('OrgVDCGroupID').values():
            policyResponseDict = self.getDfwPolicy(dcGroupId)
            if policyResponseDict is None:
                raise Exception('DFW policy not found')

            dfwURL = '{}{}{}{}'.format(
                vcdConstants.OPEN_API_URL.format(self.ipAddress),
                vcdConstants.GET_VDC_GROUP_BY_ID.format(dcGroupId),
                vcdConstants.ENABLE_DFW_POLICY,
                vcdConstants.GET_DFW_RULES.format(policyResponseDict['defaultPolicy']['id']))

            dfwURLs[dcGroupId] = dfwURL

        return dfwURLs

    def getDfwPolicy(self, orgvDCgroupId):
        """
        Description :   Get dfw policies by vdc group ID
        Parameters  :   orgvDCgroupId - Id of DC group (STR)
        Returns     :   Policy configured on DC group (DICT)
        """
        # URL to get dfw policies by vdc group ID
        url = '{}{}{}'.format(
            vcdConstants.OPEN_API_URL.format(self.ipAddress),
            vcdConstants.GET_VDC_GROUP_BY_ID.format(orgvDCgroupId),
            vcdConstants.ENABLE_DFW_POLICY)
        header = {'Authorization': self.headers['Authorization'],
                  'Accept': vcdConstants.VCD_API_HEADER}
        response = self.restClientObj.get(url, header)
        if response.status_code == requests.codes.ok:
            return response.json()

        raise Exception('Failed to get DFW policies - {}'.format(response.json()['message']))

    def getDfwPolicyRules(self, dfwURL):
        """
        Description :   Get DFW policy rules present on DC group
        Parameters  :   dfwURL : URL of DFW policy by ID (STR)
        Returns     :   List of rules present (LIST)
        """
        header = {'Authorization': self.headers['Authorization'],
                  'Accept': vcdConstants.VCD_API_HEADER}
        # get api call to retrieve firewall info of target edge gateway
        response = self.restClientObj.get(dfwURL, header)
        if response.status_code == requests.codes.ok:
            # successful retrieval of firewall info
            responseDict = response.json()
            return responseDict['values']

        raise Exception('Failed to get previously configured rules - {}'.format(response.json()['message']))

    def putDfwPolicyRules(
            self, dfwURL, payloadDict, ruleName, dcGroupId, sourceFirewallGroupObjects=None,
            destFirewallGroupObjects=None, defaultRule=False):
        """
        Description :   Create DFW policy rule by appending exsisting rules. If default rule is getting configured,
                        remove all existing rules and put only default.
        Parameters  :   dfwURL - URL of DFW policy by ID (STR)
                        payloadDict - payload to create a DFW rule (DICT)
                        ruleName - Rule from source side (DICT)
                        dcGroupId - DC group where rule is getting created (STR)
                        multipleL7 - Specifies rule has L7 profiles (BOOL)
                        defaultRule - True when default rule is configured (BOOL)
        """
        # Creating new variable for payload as with threading reference of payloadDict remains same for each thread
        payload = {
            'sourceFirewallGroups': sourceFirewallGroupObjects.get(dcGroupId) if sourceFirewallGroupObjects else None,
            'destinationFirewallGroups': destFirewallGroupObjects.get(dcGroupId) if destFirewallGroupObjects else None,
            **payloadDict
        }

        # When default rule is getting configured, do not collect existing rules.
        # Default rule will replace all previous rules
        userDefinedRulesList = [] if defaultRule else self.getDfwPolicyRules(dfwURL)

        # If default rule is already configured, put new rule before default rule otherwise put new rule in the end
        if self.rollback.apiData.get('DfwDefaultRule', {}).get(dcGroupId):
            userDefinedRulesList.insert(-1, payload)
        else:
            userDefinedRulesList.append(payload)

        self.headers['Content-Type'] = 'application/json'
        response = self.restClientObj.put(
            dfwURL, self.headers,
            data=json.dumps({'values': userDefinedRulesList})
        )
        if not response.status_code == requests.codes.accepted:
            raise Exception('Failed to create DFW rule on target - {}'.format(response.json()['message']))

        self._checkTaskStatus(taskUrl=response.headers['Location'])
        logger.debug('DFW rule {} created successfully on {}.'.format(ruleName, dcGroupId))

    def putDfwMultipleL7Rules(
            self, networkContextProfilesList, dfwURL, payloadDict, ruleName, dcGroupId,
            sourceFirewallGroupObjects, destFirewallGroupObjects):
        for networkContextProfiles in networkContextProfilesList:
            payload = {**payloadDict}
            payload['networkContextProfiles'] = [networkContextProfiles]
            self.putDfwPolicyRules(
                dfwURL, payload, ruleName, dcGroupId, sourceFirewallGroupObjects, destFirewallGroupObjects)
        logger.debug('DFW rule {} with multiple L7 service created successfully on {}.'.format(
            ruleName, dcGroupId))

    def deleteDfwPolicyRules(self, dfwURL, dcGroupId):
        """
        Description :   Delete all DFW policy rules
        Parameters  :   dfwURL - URL of DFW policy by ID (STR)
                        dcGroupId - DC group where rule is getting created (STR)
        """
        self.headers['Content-Type'] = 'application/json'
        response = self.restClientObj.put(
            dfwURL, self.headers,
            data=json.dumps({'values': []})
        )
        if not response.status_code == requests.codes.accepted:
            raise Exception('Failed to delete DFW rules on target - {}'.format(response.json()['message']))

        self._checkTaskStatus(taskUrl=response.headers['Location'])
        logger.debug(f"All DFW rules deleted from {dcGroupId}")

    def configureDfwDefaultRule(self, vcdObjList, sourceOrgVDCId):
        """
        Description :   Configure DFW default rule on DC groups associated with Org VDC.
        Parameters  :   vcdObjList - List of objects of vcd operations class (LIST)
                        sourceOrgVDCId - ID of source orgVDC(NSX ID format not URN) (STR)
        """
        try:
            if not self.rollback.apiData.get('OrgVDCGroupID'):
                return

            # Acquire lock as dc groups can be common in different org vdc's
            self.lock.acquire(blocking=True)

            dcGroupIds = self.rollback.apiData['OrgVDCGroupID'].values()
            rule = self.getDistributedFirewallRules(sourceOrgVDCId, ruleType='default')
            if not rule:
                logger.debug(f'Default rule not present on {sourceOrgVDCId}')
                return

            logger.info('Configuring DFW default rule')
            sharedNetwork = False
            networks = self.getOrgVDCNetworks(
                sourceOrgVDCId, orgVDCNetworkType='sourceOrgVDCNetworks', sharedNetwork=True, dfwStatus=True,
                saveResponse=False)
            for network in networks:
                if network['shared']:
                    sharedNetwork = True
                    break

            payloadDict = {
                'name': 'Default',
                'enabled': 'true' if rule['@disabled'] == 'false' else 'false',
                'action': 'ALLOW' if rule['action'] == 'allow' else 'DROP',
                'logging': 'true' if rule['@logged'] == 'true' else 'false',
                'ipProtocol':
                    'IPV4' if rule['packetType'] == 'ipv4'
                    else 'IPV6' if rule['packetType'] == 'ipv6'
                    else 'IPV4_IPV6',
                'direction':
                    'OUT' if rule['direction'] == 'out'
                    else 'IN' if rule['direction'] == 'in'
                    else 'IN_OUT',
            }
            for dcGroupId in dcGroupIds:
                if self.rollback.apiData.get('DfwDefaultRule', {}).get(dcGroupId):
                    continue

                policyResponseDict = self.getDfwPolicy(dcGroupId)
                if policyResponseDict is None:
                    continue

                dfwURL = '{}{}{}{}'.format(
                    vcdConstants.OPEN_API_URL.format(self.ipAddress),
                    vcdConstants.GET_VDC_GROUP_BY_ID.format(dcGroupId),
                    vcdConstants.ENABLE_DFW_POLICY,
                    vcdConstants.GET_DFW_RULES.format(policyResponseDict['defaultPolicy']['id']))

                self.putDfwPolicyRules(dfwURL, payloadDict, 'Default', dcGroupId, defaultRule=True)

                # If shared network is enabled, update metadata for each participating VDC
                if sharedNetwork:
                    for vcdObj in vcdObjList:
                        if not vcdObj.rollback.apiData.get('DfwDefaultRule'):
                            vcdObj.rollback.apiData['DfwDefaultRule'] = dict()
                        vcdObj.rollback.apiData['DfwDefaultRule'][dcGroupId] = True
                        vcdObj.saveMetadataInOrgVdc(force=True)
                else:
                    if not self.rollback.apiData.get('DfwDefaultRule'):
                        self.rollback.apiData['DfwDefaultRule'] = dict()
                    self.rollback.apiData['DfwDefaultRule'][dcGroupId] = True
                    self.saveMetadataInOrgVdc(force=True)

        except DfwRulesAbsentError as e:
            logger.debug(e)

        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def fetchFirewallGroups(self):
        """
        Description: Fetch all the firewall groups from vCD
        """
        try:
            firewallGroupsUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                              vcdConstants.FIREWALL_GROUPS_SUMMARY)
            response = self.restClientObj.get(firewallGroupsUrl, self.headers)
            # Fetching firewall groups summary
            firewallGroupsSummary = []
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved firewall groups details successfully")
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
                pageNo = 1
                pageSizeCount = 0
            else:
                response = response.json()
                raise Exception(
                    "Failed to fetch firewall group summary from target - {}".format(response['message']))

            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}?page={}&pageSize={}".format(firewallGroupsUrl, pageNo,
                                                      vcdConstants.FIREWALL_GROUPS_SUMMARY_PAGE_SIZE)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    firewallGroupsSummary.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('firewall group summary result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    response = response.json()
                    raise Exception(
                        "Failed to fetch firewall group summary from target - {}".format(response['message']))
            return firewallGroupsSummary
        except:
            raise

    def fetchFirewallGroupsByDCGroup(self):
        """
        Description: Fetch all the firewall groups from vCD
        """
        orgvDCgroupIds = self.rollback.apiData['OrgVDCGroupID'].values() if self.rollback.apiData.get(
            'OrgVDCGroupID') else []

        firewallGroupsSummary = self.fetchFirewallGroups()
        firewallGroupsSummary = list(filter(
            lambda firewallGroup: firewallGroup['ownerRef']['id'] in orgvDCgroupIds, firewallGroupsSummary
        ))

        firewallGroups = defaultdict(dict)
        for group in firewallGroupsSummary:
            firewallGroups[group['ownerRef']['id']].update({
                f"{group['typeValue']}-{group['name']}": {
                    'id': group['id']
                }
            })

        return firewallGroups

    def getTargetSecurityTags(self, vmDetails=True):
        """
        Description : Fetch all the security tags from target
        Parameters  : vmDetails - False when only tag names are to be listed and associated VMs will not be collected.
        """
        if float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA):
            return

        base_url = "{}securityTags/values?links=true".format(
            vcdConstants.OPEN_API_URL.format(self.ipAddress))
        headers = {
            'X-VMWARE-VCLOUD-TENANT-CONTEXT': self.rollback.apiData['Organization']['@id'].split(':')[-1],
            **self.headers
        }
        response = self.restClientObj.get(base_url, headers)
        # Fetching security tags summary
        securityTags = []
        if response.status_code == requests.codes.ok:
            logger.debug("Retrieved security tags details successfully")
            responseDict = response.json()
            resultTotal = responseDict['resultTotal']
            pageNo = 1
            pageSizeCount = 0
        else:
            response = response.json()
            raise Exception(
                "Failed to fetch security tag summary from target - {}".format(response['message']))

        while resultTotal > 0 and pageSizeCount < resultTotal:
            url = "{}?page={}&pageSize={}".format(base_url, pageNo,
                                                  vcdConstants.FIREWALL_GROUPS_SUMMARY_PAGE_SIZE)
            response = self.restClientObj.get(url, headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                securityTags.extend(responseDict['values'])
                pageSizeCount += len(responseDict['values'])
                logger.debug('security tag summary result pageSize = {}'.format(pageSizeCount))
                pageNo += 1
                resultTotal = responseDict['resultTotal']
            else:
                response = response.json()
                raise Exception(
                    "Failed to fetch security tag summary from target - {}".format(response['message']))

        if vmDetails:
            self.dfwSecurityTags.update({
                tag['tag']: self.getTargetSecurityTagMembers(tag['tag'])
                for tag in securityTags
            })
        else:
            self.dfwSecurityTags.update({
                tag['tag']: None
                for tag in securityTags
            })
        return self.dfwSecurityTags

    def getTargetSecurityTagMembers(self, tagName):
        """
        Description: Fetch all the associated VMs with tag from vCD
        """
        base_url = "{}securityTags/entities".format(
            vcdConstants.OPEN_API_URL.format(self.ipAddress))
        query_filter = f'&filterEncoded=true&filter=tag=={tagName}'
        url = f"{base_url}?page=1&pageSize={vcdConstants.FIREWALL_GROUPS_SUMMARY_PAGE_SIZE}{query_filter}"
        response = self.restClientObj.get(url, self.headers)

        # Fetching associated VMs with tag summary
        vms = []
        if response.status_code == requests.codes.ok:
            logger.debug(f"Retrieved associated VMs with tag {tagName} details successfully")
            responseDict = response.json()
            resultTotal = responseDict['resultTotal']
            pageNo = 1
            pageSizeCount = 0
        else:
            response = response.json()
            raise Exception("Failed to fetch associated VMs with tag {} summary from target - {}".format(
                tagName, response['message']))

        while resultTotal > 0 and pageSizeCount < resultTotal:
            url = f"{base_url}?page={pageNo}&pageSize={vcdConstants.FIREWALL_GROUPS_SUMMARY_PAGE_SIZE}{query_filter}"
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                vms.extend(responseDict['values'])
                pageSizeCount += len(responseDict['values'])
                logger.debug('associated VMs with tag {} summary result pageSize = {}'.format(
                    tagName, pageSizeCount))
                pageNo += 1
                resultTotal = responseDict['resultTotal']
            else:
                response = response.json()
                raise Exception("Failed to fetch associated VMs with tag {} summary from target - {}".format(
                    tagName, response['message']))

        return [vm['id'] for vm in vms]

    def deleteSecurityTag(self, name):
        """
        Description :   Delete security tag
        Parameters  :   name - name of tag to be deleted (STR)
        """
        url = f"{vcdConstants.OPEN_API_URL.format(self.ipAddress)}securityTags/tag"
        payload = json.dumps({
            'tag': name,
            'entities': [],
        })
        self.headers['Content-Type'] = 'application/json'
        response = self.restClientObj.put(url, self.headers, data=payload)
        if response.status_code == requests.codes.no_content:
            logger.debug(f"Security Tag deleted: {name}")
        else:
            raise Exception(f"Failed to delete Security Tag '{name}': {response.json()['message']}")

    def putSecurityTag(self, name, vms):
        """
        Description :   Create security tag
        Parameters  :   name - name of tag to be created (STR)
                        vms - list of VMs to be associated with tag (LIST)
        """
        if name in self.dfwSecurityTags:
            return

        url = f"{vcdConstants.OPEN_API_URL.format(self.ipAddress)}securityTags/tag"
        payload = json.dumps({
            'tag': name,
            'entities': vms,
        })
        self.headers['Content-Type'] = 'application/json'
        response = self.restClientObj.put(url, self.headers, data=payload)
        if response.status_code == requests.codes.no_content:
            logger.debug(f"Security Tag created: {name}")
            self.dfwSecurityTags[name] = vms
            self.rollback.apiData['SecurityTags'] = self.rollback.apiData.get('SecurityTags', []) + [name]
            self.saveMetadataInOrgVdc()
        else:
            raise Exception(f"Failed to create Security Tag '{name}': {response.json()['message']}")

    @isSessionExpired
    def securityTagsRollback(self):
        """
        Description :   Rollback task to delete all security tags
        """
        if not (
                isinstance(self.rollback.metadata.get("configureTargetVDC", {}).get("configureDFW"), bool) or
                isinstance(self.rollback.metadata.get("configureTargetVDC", {}).get("configureSecurityTags"), bool)):
            return

        if not self.rollback.apiData.get('SecurityTags'):
            return

        logger.info('Removing DFW security tags')
        for tag in self.rollback.apiData['SecurityTags']:
            self.deleteSecurityTag(tag)

    @description('Creating DFW security tags')
    @remediate
    def configureSecurityTags(self):
        """
        Create source Security tags on target side
        """
        if float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA):
            return

        logger.info('Creating DFW security tags')
        sourceOrgVdcName = self.rollback.apiData['sourceOrgVDC']['@name']
        securityTags = self.getSourceDfwSecurityTags()
        for tag in securityTags.values():
            self.putSecurityTag(f"{sourceOrgVdcName}_{tag['name']}", tag['members'])

    @isSessionExpired
    def getSourceDfwSecurityTags(self, vmDetails=True):
        """
        Description : Fetch all the security tags from source
        Parameters  : vmDetails - False when only tag names are to be listed and associated VMs will not be collected.
        """
        logger.debug('Fetching DFW security tags')
        url = "{}{}".format(
            vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
            'services/securitytags/tag/scope/{}'.format(self.rollback.apiData['sourceOrgVDC']['@id'].split(':')[-1])
        )
        response = self.restClientObj.get(url, self.headers)
        responseDict = xmltodict.parse(response.content)
        if not response.status_code == requests.codes.ok:
            raise Exception('Unable to fetch Security Tags {}'.format(responseDict.get('Error', {}).get('@message')))

        securityTags = []
        if responseDict.get('securityTags'):
            securityTags = (
                responseDict['securityTags']['securityTag']
                if isinstance(responseDict['securityTags']['securityTag'], list)
                else [responseDict['securityTags']['securityTag']])

        securityTags = {
            tag['objectId']: {
                'id': tag['objectId'],
                'name': tag['name'],
                'members': self.getSourceSecurityTagMembers(tag['objectId'], tag['name']) if vmDetails else None,
            }
            for tag in securityTags
        }
        return securityTags

    @isSessionExpired
    def getSourceSecurityTagMembers(self, tag_id, tag_name):
        """
        Collects members associated with source security tag
        """
        url = "{}{}".format(
            vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
            'services/securitytags/tag/{}/vm'.format(tag_id)
        )

        response = self.restClientObj.get(url, self.headers)
        responseDict = xmltodict.parse(response.content)
        if not response.status_code == requests.codes.ok:
            raise Exception(f"Unable to fetch Security Tag {tag_name}: {responseDict.get('Error', {}).get('@message')}")

        if responseDict.get('basicinfolist'):
            vms = (
                responseDict['basicinfolist']['basicinfo']
                if isinstance(responseDict['basicinfolist']['basicinfo'], list)
                else [responseDict['basicinfolist']['basicinfo']])
            return [
                f"urn:vcloud:vm:{vm['objectId']}"
                for vm in vms
                if vm['objectTypeName'] == 'VirtualMachine'
            ]
        return []

    def createDfwFirewallGroup(self, payload, allFirewallGroups, groupType, ruleObjects):
        """
        Description :   Create a firewall group(Static/dynamic/ipset) on DC group
        Parameters  :   payload : details of firewall group to be created (DICT)
                        allFirewallGroups : List of all existing firewall groups (LIST)
                        groupType : Type of group as identified by API response
        Returns     :   ID of firewall group created
        """
        # Skip if firewall groups is already present
        nameKey = f"{groupType}-{payload['name']}"
        if allFirewallGroups[payload['ownerRef']['id']].get(nameKey):
            logger.debug(f"Firewall group already present: {payload['name']} on {payload['ownerRef']['id']}")
            ruleObjects.append({'id': allFirewallGroups[payload['ownerRef']['id']][nameKey]['id']})
            return

        firewallGroupUrl = "{}{}".format(
            vcdConstants.OPEN_API_URL.format(self.ipAddress),
            vcdConstants.CREATE_FIREWALL_GROUP)
        self.headers['Content-Type'] = 'application/json'
        response = self.restClientObj.post(firewallGroupUrl, self.headers, data=json.dumps(payload))

        if response.status_code == requests.codes.accepted:
            firewallGroup = self._checkTaskStatus(taskUrl=response.headers['Location'], returnOutput=True)
            logger.debug(f"Firewall Group created: {payload['name']}({firewallGroup}) on {payload['ownerRef']['id']}")
            allFirewallGroups[payload['ownerRef']['id']].update({
                nameKey: {'id': f'urn:vcloud:firewallGroup:{firewallGroup}'}
            })
            ruleObjects.append({'id': f"urn:vcloud:firewallGroup:{firewallGroup}"})
            return

        raise Exception('Failed to create Firewall group - {}'.format(response.json()['message']))

    @description('Increase the scope of the network to OrgVDC group')
    @remediate
    def increaseScopeforNetworks(self, rollback=False):
        """
        Description: Increase the scope of the network to OrgVDC group
        parameter:  rollback- True to decrease the scope of networks from NSX-T ORg VDC
        """
        try:
            # Acquiring thread lock
            self.lock.acquire(blocking=True)
            # Check if services configuration or network switchover was performed or not
            if rollback and not self.rollback.metadata.get("configureTargetVDC", {}).get("increaseScopeforNetworks"):
                return

            targetOrgVdcId = self.rollback.apiData['targetOrgVDC']['@id']
            ownerRefID = self.rollback.apiData['OrgVDCGroupID'] if self.rollback.apiData.get('OrgVDCGroupID') else {}
            targetNetworks = self.retrieveNetworkListFromMetadata(targetOrgVdcId, dfwStatus=True, orgVDCType='target')
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            sourceNetworks = self.retrieveNetworkListFromMetadata(sourceOrgVDCId, dfwStatus=True, orgVDCType='source')
            allLayer3Rules = self.getDistributedFirewallConfig(sourceOrgVDCId)
            if ownerRefID:
                if rollback:
                    logger.info("Rollback: Decreasing scope of networks")
                else:
                    logger.info('Increasing scope of networks')
                for targetNetwork in targetNetworks:
                    for sourceNetwork in sourceNetworks:
                        if sourceNetwork['name'] + '-v2t' == targetNetwork['name']:
                            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                vcdConstants.GET_ORG_VDC_NETWORK_BY_ID.format(targetNetwork['id']))
                            header = {'Authorization': self.headers['Authorization'],
                                      'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
                            response = self.restClientObj.get(url, header)
                            if response.status_code == requests.codes.ok:
                                responseDict = response.json()
                                if targetNetwork['networkType'] == 'ISOLATED' or sourceNetwork["networkType"] == 'DIRECT':
                                    # rollback is true to decrease the scope of ORG VDC networks
                                    if rollback:
                                        # Decrease scope only if it was increased for Direct Networks
                                        if sourceNetwork['networkType'] == 'DIRECT':
                                            if not ownerRefID.get(targetNetwork['id']):
                                                continue
                                        # changing the owner reference from  org VDC group to org VDC
                                        responseDict['ownerRef'] = {'id': targetOrgVdcId}
                                    else:
                                        # Increase scope of network only if the network is shared or DFW is configured
                                        if allLayer3Rules or sourceNetwork['shared']:
                                            # Increase Direct Network scope only if it was created in PG backed external network
                                            if sourceNetwork["networkType"] == "DIRECT" and ownerRefID.get(
                                                    targetNetwork['id']):
                                                responseDict['ownerRef'] = {'id': ownerRefID[targetNetwork['id']]}
                                            elif allLayer3Rules and sourceNetwork["networkType"] != "DIRECT":
                                                # changing the owner reference from org VDC to org VDC group
                                                responseDict['ownerRef'] = {'id': list(ownerRefID.values())[0] if targetNetwork['id'] not in list(ownerRefID.keys()) else ownerRefID[targetNetwork['id']]}
                                            elif ownerRefID.get(targetNetwork['id']):
                                                # changing the owner reference from org VDC to org VDC group
                                                responseDict['ownerRef'] = {'id': ownerRefID[targetNetwork['id']]}

                                    payloadData = json.dumps(responseDict)
                                    self.headers['Content-Type'] = 'application/json'
                                    # post api call to create firewall group
                                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                                    if response.status_code == requests.codes.accepted:
                                        # successful creation of firewall group
                                        taskUrl = response.headers['Location']
                                        self._checkTaskStatus(taskUrl, returnOutput=False)
                                        logger.debug('The network - {} scope has been changed successfully'.format(responseDict['name']))
                                    else:
                                        errorResponse = response.json()
                                        # failure in increase scope of the network
                                        raise Exception('Failed to change scope of the network {} - {}'.format(responseDict['name'], errorResponse['message']))
                            else:
                                responseDict = response.json()
                                raise Exception('Failed to retrieve network- {}'.format(responseDict['message']))
        except:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def configureDFWgroups(
            self, entities, ruleId, allFirewallGroups, appliedToDcGroups, sourceToTargetOrgNetIds,
            targetEntitiesToDcGroupMap, sourceDfwSecurityGroups, source=True):
        """
        Description: Configures Firewall group in VDC group
        parameters: entities: This refers source or destination in a DFW rule
                    l3rule: L3 dfw rule
                    source: True/False to denote the entities is for source or destination
                   firewallIdDict:
                   allFirewallGroups: (DICT) key - DC group ID, value - List of firewall groups
        """
        def _listify(_list):
            """Converts to list if not a list"""
            return _list if isinstance(_list, list) else [_list]

        def createVmTagName(name, value):
            """Validates if length of tag name does not exceed max limit"""
            if len(name) > 128 - len(value):
                logger.debug('Slicing tag name as its length exceeded')
                return "{}[TRIMMED]-{}".format(name[:127 - len(f'[TRIMMED]-{value}')], value)
            return f'{name}-{value}'

        def createFirewallGroupName(
                orgVdcName, orgVdcId, sourceGroupName, sourceGroupId, ruleId=None, source=None,
                groupType=None, idx=None):
            """Validates if length of firewall group name does not exceed max limit"""
            # IP Set        :   f"{orgVdcName}-{sourceGroupName}"
            # Security Group:   f"{orgVdcName}-{sourceGroupName}-{groupType}-{idx}"
            #                   f"{orgVdcName}-{sourceGroupName}-{idx}",
            # VDC Networks  :   f"{orgVdcName}-{sourceGroupName}"   # Not implemented
            # Ipv4Address   :   f"{orgVdcName}-{ruleId}-{groupType}-{'S' if source else 'D'}"

            suffix = ''
            if ruleId:
                if not sourceGroupName:
                    sourceGroupName = ruleId
                else:
                    suffix = f"{suffix}_{ruleId}"
            if groupType:
                suffix = f"{suffix}_{groupType}"
            if isinstance(source, bool):
                suffix = f"{suffix}_{'S' if source else 'D'}"
            if isinstance(idx, int):
                suffix = f"{suffix}_{idx}"

            name = f"{orgVdcName}_{sourceGroupName}{suffix}"
            if len(name) < 128:
                return name

            name = f"vdc-{orgVdcId}_{sourceGroupName}{suffix}"
            if len(name) < 128:
                return name

            return f"vdc-{orgVdcId}_group-{sourceGroupId}{suffix}"

        ipv4Addresses = list()
        firewallGroupObjects = defaultdict(list)
        orgVdcName = self.rollback.apiData['sourceOrgVDC']['@name']
        orgVdcId = self.rollback.apiData['sourceOrgVDC']['@id'].split(':')[-1]
        groupTypes = {
            'Ipv4Address': 'IP_SET',
            'IPSet': 'IP_SET',
            'Network': 'STATIC_MEMBERS',
            'VirtualMachine': 'VM_CRITERIA',
        }

        for entity in entities:
            # Collect all Ipv4Address from rule and create a single group on target side.
            if entity['type'] == 'Ipv4Address':
                ipv4Addresses.append(entity['value'])

            elif entity['type'] == 'IPSet':
                ipset = self.getIpset(entity['value'])
                for dcGroupId in appliedToDcGroups:
                    payload = {
                        'name': createFirewallGroupName(orgVdcName, orgVdcId, entity['name'], entity['value']),
                        'ownerRef': {'id': dcGroupId},
                        'ipAddresses': ipset['ipset']['value'].split(','),
                    }
                    self.thread.spawnThread(
                        self.createDfwFirewallGroup, payload, allFirewallGroups, groupTypes.get(entity['type']),
                        firewallGroupObjects[dcGroupId])
                # Halting the main thread till all the threads have completed their execution
                self.thread.joinThreads()

            elif entity['type'] == 'Network':
                network_id = sourceToTargetOrgNetIds.get(entity['value'])
                ownerRefId = list(targetEntitiesToDcGroupMap[network_id])[0]
                payload = {
                    'name': f"SecurityGroup-({entity['name']})",
                    'ownerRef': {'id': ownerRefId},
                    'members': [{'id': network_id}],
                    'type': vcdConstants.SECURITY_GROUP,
                }
                self.createDfwFirewallGroup(payload, allFirewallGroups, groupTypes.get(entity['type']), firewallGroupObjects[ownerRefId])

            elif entity['type'] == 'VirtualMachine':
                tagName = createVmTagName(entity['name'], entity['value'])
                self.putSecurityTag(tagName, [entity['value']])
                vmCriteria = [{
                    'rules': [{
                        'attributeType': 'VM_TAG',
                        'operator': 'EQUALS',
                        'attributeValue': tagName,
                    }]
                }]
                for dcGroupId in appliedToDcGroups:
                    payload = {
                        'name': tagName,
                        'description': '',
                        'vmCriteria': vmCriteria,
                        'ownerRef': {'id': dcGroupId},
                        'typeValue': 'VM_CRITERIA',
                    }
                    self.thread.spawnThread(
                        self.createDfwFirewallGroup, payload, allFirewallGroups, groupTypes.get('VirtualMachine'),
                        firewallGroupObjects[dcGroupId])
                # Halting the main thread till all the threads have completed their execution
                self.thread.joinThreads()

            elif entity['type'] == 'SecurityGroup':
                sourceGroup = sourceDfwSecurityGroups[entity['value']]
                if sourceGroup.get('member'):
                    includeMembers = (
                        sourceGroup['member']
                        if isinstance(sourceGroup['member'], list)
                        else [sourceGroup['member']])
                    vmTags = []
                    for member in includeMembers:
                        if member['type']['typeName'] == 'VirtualMachine':
                            tagName = createVmTagName(member['name'], member['objectId'])
                            self.putSecurityTag(tagName, [member['objectId']])
                            vmTags.append(tagName)

                        if member['type']['typeName'] == 'SecurityTag':
                            vmTags.append(f"{orgVdcName}_{member['name']}")

                        if member['type']['typeName'] == 'IPSet':
                            ipset = self.getIpset(member['objectId'])
                            for dcGroupId in appliedToDcGroups:
                                payload = {
                                    'name': createFirewallGroupName(
                                        orgVdcName, orgVdcId, member['name'], member['objectId']),
                                    'ownerRef': {'id': dcGroupId},
                                    'ipAddresses': ipset['ipset']['value'].split(','),
                                }
                                self.thread.spawnThread(
                                    self.createDfwFirewallGroup, payload, allFirewallGroups,
                                    groupTypes.get(member['type']['typeName']), firewallGroupObjects[dcGroupId])
                            # Halting the main thread till all the threads have completed their execution
                            self.thread.joinThreads()

                        if member['type']['typeName'] == 'Network':
                            network_id = sourceToTargetOrgNetIds.get(member['objectId'])
                            ownerRefId = list(targetEntitiesToDcGroupMap[network_id])[0]
                            payload = {
                                'name': f"SecurityGroup-({member['name']})",
                                'ownerRef': {'id': ownerRefId},
                                'members': [{'id': network_id}],
                                'type': vcdConstants.SECURITY_GROUP,
                            }
                            self.createDfwFirewallGroup(
                                payload, allFirewallGroups, groupTypes.get(member['type']['typeName']),
                                firewallGroupObjects[ownerRefId])

                    if vmTags:
                        vmCriteria = [
                            {
                                'rules': [{
                                    'attributeType': 'VM_TAG',
                                    'operator': 'EQUALS',
                                    'attributeValue': tagName,
                                }]
                            }
                            for tagName in vmTags
                        ]
                        for dcGroupId in appliedToDcGroups:
                            for idx, sublist in chunksOfList(vmCriteria, 3):
                                payload = {
                                    'name': createFirewallGroupName(
                                        orgVdcName, orgVdcId, sourceGroup['name'], entity['value'], groupType='member',
                                        idx=idx),
                                    'description': sourceGroup['description'],
                                    'vmCriteria': sublist,
                                    'ownerRef': {'id': dcGroupId},
                                    'typeValue': 'VM_CRITERIA',
                                }
                                self.thread.spawnThread(
                                    self.createDfwFirewallGroup, payload, allFirewallGroups,
                                    groupTypes.get('VirtualMachine'), firewallGroupObjects[dcGroupId])
                        # Halting the main thread till all the threads have completed their execution
                        self.thread.joinThreads()

                if sourceGroup.get('dynamicMemberDefinition'):
                    vmCriteria = [
                        {
                            'rules': [
                                {
                                    'attributeType':
                                        'VM_TAG' if rule['key'] == 'VM.SECURITY_TAG'
                                        else 'VM_NAME' if rule['key'] == 'VM.NAME'
                                        else None,
                                    'operator':
                                        'CONTAINS' if rule['criteria'] == 'contains'
                                        else 'STARTS_WITH' if rule['criteria'] == 'starts_with'
                                        else 'ENDS_WITH' if rule['criteria'] == 'ends_with'
                                        else None,
                                    'attributeValue':
                                        f"{orgVdcName}_{rule['value']}"
                                        if rule['key'] == 'VM.SECURITY_TAG' and rule['criteria'] == 'starts_with'
                                        else rule['value'],
                                }
                                for rule in _listify(dynset['dynamicCriteria'])
                            ]
                        }
                        for dynset in _listify(sourceGroup['dynamicMemberDefinition']['dynamicSet'])
                    ]
                    for dcGroupId in appliedToDcGroups:
                        for idx, sublist in chunksOfList(vmCriteria, 3):
                            payload = {
                                'name': createFirewallGroupName(
                                        orgVdcName, orgVdcId, sourceGroup['name'], entity['value'], idx=idx),
                                'description': sourceGroup['description'],
                                'vmCriteria': sublist,
                                'ownerRef': {'id': dcGroupId},
                                'typeValue': 'VM_CRITERIA',
                            }
                            self.thread.spawnThread(
                                self.createDfwFirewallGroup, payload, allFirewallGroups,
                                groupTypes.get('VirtualMachine'), firewallGroupObjects[dcGroupId])
                    # Halting the main thread till all the threads have completed their execution
                    self.thread.joinThreads()

        if ipv4Addresses:
            for dcGroupId in appliedToDcGroups:
                payload = {
                    'name': createFirewallGroupName(
                        orgVdcName, orgVdcId, sourceGroupName=None, sourceGroupId=None, ruleId=ruleId,
                        groupType='ip', source=source),
                    'ownerRef': {'id': dcGroupId},
                    'ipAddresses': ipv4Addresses
                }
                self.thread.spawnThread(
                    self.createDfwFirewallGroup, payload, allFirewallGroups, groupTypes.get('Ipv4Address'),
                    firewallGroupObjects[dcGroupId])
            # Halting the main thread till all the threads have completed their execution
            self.thread.joinThreads()

        return firewallGroupObjects

    def getIpset(self, ipsetId):
        # url to retrieve the info of ipset group by id
        url = "{}{}".format(
            vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
            vcdConstants.GET_IPSET_GROUP_BY_ID.format(ipsetId))
        # get api call to retrieve the ipset group info
        response = self.restClientObj.get(url, self.headers)
        if response.status_code == requests.codes.ok:
            # successful retrieval of ipset group info
            responseDict = xmltodict.parse(response.content)
            return responseDict
        raise Exception('Unable to fetch ipset {} - {}'.format(ipsetId, response.json()['message']))

    @isSessionExpired
    def dfwRulesRollback(self):
        """
            Description: Removing DFW rules from datacenter group for rollback
        """
        try:
            # Check if services configuration or network switchover was performed or not
            if not isinstance(self.rollback.metadata.get("configureTargetVDC", {}).get("configureDFW"), bool) \
                    or not self.rollback.apiData.get('DfwDefaultRule'):
                return
            # If DFW was not configured on source org vdc return
            sourceOrgVDCId = self.rollback.apiData.get('sourceOrgVDC', {}).get('@id')
            if sourceOrgVDCId:
                allLayer3Rules = self.getDistributedFirewallConfig(sourceOrgVDCId)
                if not allLayer3Rules:
                    return
            # Acquiring thread lock
            self.lock.acquire(blocking=True)

            orgVDCGroupIDList = list(self.rollback.apiData['OrgVDCGroupID'].values()) if self.rollback.apiData.get('OrgVDCGroupID') else []
            # Fetching all dc group id's from vCD
            vdcGroupsIds = [group['id'] for group in self.getOrgVDCGroup()]
            if [dcGroupId for dcGroupId in orgVDCGroupIDList if dcGroupId in vdcGroupsIds]:
                logger.info('Rollback: Deleting DFW rules from Data Center Groups')
                for orgVDCGroupID in orgVDCGroupIDList:
                    policyResponseDict = self.getDfwPolicy(orgVDCGroupID)
                    policyID = policyResponseDict['defaultPolicy']['id']
                    # url to fetch dfw rules
                    dfwURL = '{}{}{}{}'.format(
                        vcdConstants.OPEN_API_URL.format(self.ipAddress),
                        vcdConstants.GET_VDC_GROUP_BY_ID.format(orgVDCGroupID),
                        vcdConstants.ENABLE_DFW_POLICY,
                        vcdConstants.GET_DFW_RULES.format(policyID))

                    self.deleteDfwPolicyRules(dfwURL, orgVDCGroupID)
                    logger.debug('Successfully removed DFW rules from datacenter groups')
        except:
            logger.error(traceback.format_exc())
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def dfwGroupsRollback(self):
        """
            Description: Removing DFW groups from datacenter group for rollback
        """
        try:
            # Check if services configuration was performed or not
            if not self.rollback.metadata.get("configureTargetVDC", {}).get("increaseScopeOfEdgegateways") or \
                    not isinstance(self.rollback.metadata.get("configureTargetVDC", {}).get("configureDFW"), bool):
                return
            # Acquiring thread lock
            self.lock.acquire(blocking=True)
            orgVDCGroupID = list(self.rollback.apiData['OrgVDCGroupID'].values()) if self.rollback.apiData.get('OrgVDCGroupID') else []
            if orgVDCGroupID:
                if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA):
                    logger.info("Rollback: Removing Firewall-Groups from Data Center Groups")
                else:
                    logger.info("Rollback: Removing Security-Groups from Data Center Groups")
                # url to fetch firewall groups summary
                firewallGroupsSummary = self.fetchFirewallGroupsByDCGroup()

                # Iterating over dfw groups to delete the groups using firewall group id
                for owner, groups in firewallGroupsSummary.items():
                    for _, sublist in chunksOfList(list(groups.items()), 40):
                        taskUrls = dict()
                        for firewallGroupName, firewallGroup in sublist:
                            deleteFirewallGroupUrl = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                   vcdConstants.FIREWALL_GROUP.format(firewallGroup['id']))
                            response = self.restClientObj.delete(deleteFirewallGroupUrl, self.headers)
                            if response.status_code == requests.codes.accepted:
                                taskUrls[firewallGroupName] = response.headers['Location']
                            else:
                                response = response.json()
                                raise Exception("Failed to delete firewall group '{}' from target - {}".format(
                                    firewallGroupName, response['message']))

                        errors = list()
                        for firewallGroupName, url in taskUrls.items():
                            try:
                                self._checkTaskStatus(taskUrl=url)
                                logger.debug("Successfully deleted firewall group '{}'".format(firewallGroupName))
                            except Exception as e:
                                errors.append(e)

                        if errors:
                            raise Exception(errors)

                logger.debug('Successfully removed Firewall-Groups from Data Center Groups')
        except:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def firewallruleRollback(self):
        """
        Description: Removing DFW rules from datacenter group for rollback
        """
        try:
            # Check if services configuration was performed or not
            if not self.rollback.metadata.get("configureTargetVDC", {}).get("increaseScopeOfEdgegateways"):
                return

            orgVDCGroupID = list(self.rollback.apiData['OrgVDCGroupID'].values()) if self.rollback.apiData.get('OrgVDCGroupID') else []
            # Fetching all dc group id's from vCD
            vdcGroupsIds = [group['id'] for group in self.getOrgVDCGroup()]

            if [dcGroupId for dcGroupId in orgVDCGroupID if dcGroupId in vdcGroupsIds]:
                targetEdgeGatewayIdList = [edgeGateway['id'] for edgeGateway in
                                           self.rollback.apiData['targetEdgeGateway']]
                for edgeGatewayId in targetEdgeGatewayIdList:
                    # url to configure firewall rules on target edge gateway
                    firewallUrl = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                  vcdConstants.ALL_EDGE_GATEWAYS,
                                                  vcdConstants.T1_ROUTER_FIREWALL_CONFIG.format(edgeGatewayId))
                    response = self.restClientObj.delete(firewallUrl, self.headers)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        self._checkTaskStatus(taskUrl=taskUrl)
                    else:
                        response = response.json()
                        raise Exception(
                            "Failed to delete firewall from target - {}".format(response['message']))
                logger.debug('Successfully deleted firewall rules')
        except Exception:
            logger.error(traceback.format_exc())
            raise