#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2013, Kieran J. Broadfoot. All rights reserved.
#

################################################################################
# Imports
################################################################################
import sys
import os
import telnetlib
import re
import time
from xml.dom.minidom import parseString
import StringIO

################################################################################
# Globals
################################################################################

########################################
def updateVar(name, value, folder=0):
	if name not in indigo.variables:
		indigo.variable.create(name, value=value, folder=folder)
	else:
		indigo.variable.updateValue(name, value)

################################################################################
class Plugin(indigo.PluginBase):
	########################################
	# Class properties
	########################################
	
	########################################
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs): 
		indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		self.cgateLocation = pluginPrefs.get("cgateNetworkLocation", "127.0.0.1")
		self.cbusNetwork = pluginPrefs.get("cbusNetwork", "254")
		self.cbusLightingMap = {}
		self.cbusUnitMap = {}
	
	########################################
	def __del__(self):
		indigo.PluginBase.__del__(self)
		
	########################################
	def startup(self):
		indigo.server.log("starting c-bus plugin")
		self.connection = telnetlib.Telnet(self.cgateLocation, 20023)
		self.initConnection()
		self.getReadyState()

		self.generateGroupData()
		self.generateDeviceTypesPerGroup()

		self.mapDevices()
		self.createDevices()

	def shutdown(self):
		indigo.server.log("stopping c-bus plugin")
		self.connection.close()
		pass
		
	########################################
	def runConcurrentThread(self):
		indigo.server.log("starting c-bus monitoring thread")
		try:
			timeSinceLastIndigoDeviceQuery = None
			self.monitor = telnetlib.Telnet(self.cgateLocation, 20025)
			while True:
				data = self.readUntil(self.monitor, ".*\n")
				# we might receive multiple lines in this data string.
				for line in data.split('\n'):
					m = re.match("lighting\s(\w+)\s\/\/\w+\/([\w|\/]+)\s+([\w|\#]+).*", line)
					# group 1 is action, 2 is qualified address, 3 is brightness value
					if m:
						try: 
							device = None
							for dev in indigo.devices.iter("self"):
								if dev.address == m.group(2):
									device = indigo.devices[dev.name]
							if device != None:
								level = 0
								state = False
								if m.group(1) == 'on':
									level = 100
									state = True
								if m.group(1) == 'off':
									level = 0
								if m.group(1) == 'ramp':
									if m.group(3) == '0':
										state = False
										level = 0
									else:
										state = True
										level = self.valueToIndigo(m.group(3))
								device.updateStateOnServer(key='brightnessLevel', value=level)
								device.updateStateOnServer(key='onOffState', value=state)
						except:
							pass
				
		except self.StopThread:
			pass
		
	def stopConcurrentThread(self):
		self.monitor.close()
			
	########################################
	# c-bus specific methods below
	
	def initConnection(self):
		self.readUntil(self.connection, "201 Service ready:.*")

	def getReadyState(self):
		ready = False
		while ready != True:
			self.writeTo(self.connection, "net list\r\n")
			networkState = self.readUntil(self.connection, "131.*")
			check = re.match(".*network="+self.cbusNetwork+" State=ok.*",networkState)
			if check:
				indigo.server.log("c-bus network ready")
				ready = True
			else:
				indigo.server.log("c-bus not yet ready. waiting 10 seconds for retry")
				time.sleep(10)

	def generateGroupData(self):
		indigo.server.log("searching for c-bus lighting groups")
		# use dbgetxml 254/56 to determine names/OID/address of each lighting group
		self.writeTo(self.connection, "dbgetxml "+self.cbusNetwork+"/56\r\n")
		xml = self.readUntil(self.connection, "344 End XML snippet")

		xml = xml.replace("343-Begin XML snippet","")
		xml = xml.replace("347-","",2)
		xml = xml.replace("344 End XML snippet","")
		xml = xml.replace("\n","",5)
		xml = xml.replace("<?xml version=\"1.0\" encoding=\"utf-8\"?>","")

		dom = parseString(xml)
		for group in dom.getElementsByTagName("Group"):
			address = group.getElementsByTagName("Address")[0].childNodes[0].data
			self.cbusLightingMap[self.cbusNetwork+"/56/"+address] = {'oid':group.getElementsByTagName("OID")[0].childNodes[0].data, 
									'name':group.getElementsByTagName("TagName")[0].childNodes[0].data,
									'unqualifiedAddress':address, 'level':'0'}

	def generateDeviceTypesPerGroup(self):
		indigo.server.log("searching for c-bus units")
		self.writeTo(self.connection, "tree "+self.cbusNetwork+"\r\n")
		treeStr = self.readUntil(self.connection, "320 -end-")
		# we are looking for two types of items.
		# Units (to determine which objects are relays and dimmers)
		# Groups (so we can match them to correct unit type)
		for line in StringIO.StringIO(treeStr):
			line = line.rstrip()
			m0 = re.match(".*p\/(\w+).*type\=(\w+).*groups\=(.*)", line)
			m1 = re.match(".*\/56\/(\w+).*level\=(\w+).*units\=(.*)", line)
			if m0:
				unitType = "unknown"
				if re.match("DIM.*",m0.group(2)):
					unitType = "cbusDimmer"
				if re.match("REL.*",m0.group(2)):
					unitType = "cbusRelay"
				self.cbusUnitMap[m0.group(1)] = { 'unit':unitType, 'groups': m0.group(3).split(',') }
			elif m1:
				if self.cbusNetwork+"/56/"+m1.group(1) in self.cbusLightingMap:
					self.cbusLightingMap[self.cbusNetwork+"/56/"+m1.group(1)]['level'] = m1.group(2)
					self.cbusLightingMap[self.cbusNetwork+"/56/"+m1.group(1)]['units'] = m1.group(3).split(',')

	def mapDevices(self):
		indigo.server.log("mapping c-bus lighting groups to channel types")
		# for each item in lighting map, find the units that supports it.  if not found set to relay
		# if found on multiple units then default to relay
		# then create the device
		for group in self.cbusLightingMap.keys():
			for unit in self.cbusUnitMap:
				if self.cbusUnitMap[unit]['unit'] == "unknown":
					continue
				if self.cbusLightingMap[group]['unqualifiedAddress'] in self.cbusUnitMap[unit]['groups']:
					# we need to account for all unit groups
					if 'type' not in self.cbusLightingMap[group]:
						self.cbusLightingMap[group]['type'] = self.cbusUnitMap[unit]['unit']
					else:
						# we've already matched before.  if unit is relay and value is dimmer replace, otherwise do nothing
						# if a group applies across multiple unit types then we need to apply the lowest common feature set to it
						# for all unit groups that is typically on/off
						if self.cbusLightingMap[group]['type'] == "dimmer" and self.cbusUnitMap[unit]['unit'] == "relay":
							self.cbusLightingMap[group]['type'] = self.cbusUnitMap[unit]['unit']

			# if we havent seen a match then really we should set to relay type however for those of us using
			# MRA like functionality (e.g. audio controls etc) then dimming functionality is required.  Should 
			# make this a configurable option?
			if 'type' not in self.cbusLightingMap[group]:
				self.cbusLightingMap[group]['type'] = "cbusDimmer"

	def createDevices(self):
		indigo.server.log("creating c-bus devices in Indigo")
		for group in self.cbusLightingMap.keys():
			device = None
			for dev in indigo.devices.iter("self"):
				# search for c-bus devices to find a match for this address. would be nicer to search by address.
				if dev.address == group:
					device = indigo.devices[dev.name]
					
			if device == None:
				device = indigo.device.create(protocol=indigo.kProtocol.Plugin,
			    	address=group,
			    	name=self.cbusLightingMap[group]['name'], 
			    	description=self.cbusLightingMap[group]['name'], 
			    	pluginId="uk.co.l1fe.indigoplugin.C-Bus",
			    	deviceTypeId=self.cbusLightingMap[group]['type'],
			    	props={"OID":self.cbusLightingMap[group]['oid'],"unqualifiedAddress":self.cbusLightingMap[group]['unqualifiedAddress']})
			if self.cbusLightingMap[group]['type'] == "cbusDimmer":
				device.updateStateOnServer(key='brightnessLevel', value=self.valueToIndigo(self.cbusLightingMap[group]['level']))
			if int(self.cbusLightingMap[group]['level']) > 0:
				device.updateStateOnServer(key='onOffState', value=True)
			else:
				device.updateStateOnServer(key='onOffState', value=False)

	def readUntil(self, connection, str, timeout=2):
		try:
			return connection.read_until(str, timeout)
		except EOFError:
			indigo.server.log("lost connection to c-gate")

	def writeTo(self, connection, str):
		connection.write(str.encode('latin-1'))
		
	def valueFromIndigo(self, value):
		return str(int(value * 2.55))

	def valueToIndigo(self, value):
		return int(int(value) / 2.55)
		
	def rampChannel(self, device, actionString, value):
		self.writeTo(self.connection,"ramp "+self.cbusNetwork+"/56/"+device.pluginProps['unqualifiedAddress']+" "+value+" 0m\r\n")
		result = self.readUntil(self.connection, "200 OK:.*")
		if result == '':
			indigo.server.log(u"send \"%s\" %s to %d failed" % (device.name, actionString, value), isError=True)
		else:
			indigo.server.log(u"sent \"%s\" %s to %d" % (device.name, actionString, int(value)))
			if int(value) > 0:
				device.updateStateOnServer("onOffState", True)
			else:
				device.updateStateOnServer("onOffState", False)
			device.updateStateOnServer("brightnessLevel", self.valueToIndigo(value))
		
	########################################
	# Relay / Dimmer Action callback
	######################
	def actionControlDimmerRelay(self, action, dev):
		###### TURN ON ######
		if action.deviceAction == indigo.kDeviceAction.TurnOn:
			# Command hardware module (dev) to turn ON here:

			self.writeTo(self.connection,"on "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
			result = self.readUntil(self.connection, "200 OK:.*")
			if result == '':
				indigo.server.log(u"send \"%s\" %s failed" % (dev.name, "on"), isError=True)
			else:
				indigo.server.log(u"sent \"%s\" %s" % (dev.name, "on"))
				dev.updateStateOnServer("onOffState", True)

		###### TURN OFF ######
		elif action.deviceAction == indigo.kDeviceAction.TurnOff:
			# Command hardware module (dev) to turn OFF here:
			
			self.writeTo(self.connection,"off "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
			result = self.readUntil(self.connection, "200 OK:.*")
			if result == '':
				indigo.server.log(u"send \"%s\" %s failed" % (dev.name, "off"), isError=True)
			else:
				indigo.server.log(u"sent \"%s\" %s" % (dev.name, "off"))
				dev.updateStateOnServer("onOffState", False)

		###### TOGGLE ######
		elif action.deviceAction == indigo.kDeviceAction.Toggle:
			# Command hardware module (dev) to toggle here:
			# ** IMPLEMENT ME **
			newOnState = not dev.onState

			if newOnState:
				self.writeTo(self.connection,"on "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
			else:
				self.writeTo(self.connection,"off "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
			result = self.readUntil(self.connection, "200 OK:.*")
			
			if result == '':
				indigo.server.log(u"send \"%s\" %s failed" % (dev.name, "toggle"), isError=True)
			else:
				indigo.server.log(u"sent \"%s\" %s" % (dev.name, "toggle"))
				dev.updateStateOnServer("onOffState", newOnState)

		###### SET BRIGHTNESS ######
		elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
			# Command hardware module (dev) to set brightness here:

			self.rampChannel(dev, "set brightness", self.valueFromIndigo(action.actionValue))
		
		###### BRIGHTEN BY ######
		elif action.deviceAction == indigo.kDeviceAction.BrightenBy:
			# Command hardware module (dev) to do a relative brighten here:

			newBrightness = dev.brightness + action.actionValue
			if newBrightness > 100:
				newBrightness = 100
				
			self.rampChannel(dev, "brighten", self.valueFromIndigo(newBrightness))

		###### DIM BY ######
		elif action.deviceAction == indigo.kDeviceAction.DimBy:
			# Command hardware module (dev) to do a relative dim here:

			newBrightness = dev.brightness - action.actionValue
			if newBrightness < 0:
				newBrightness = 0
				
			self.rampChannel(dev, "dim", self.valueFromIndigo(newBrightness))
				
		###### STATUS REQUEST ######
		elif action.deviceAction == indigo.kDeviceAction.RequestStatus:
			# Query hardware module (dev) for its current states here:
			# ** IMPLEMENT ME **
			indigo.server.log(u"sent \"%s\" %s" % (dev.name, "status request"))