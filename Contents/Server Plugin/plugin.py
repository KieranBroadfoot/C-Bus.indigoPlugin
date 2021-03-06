#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2016, Kieran J. Broadfoot. All rights reserved.
# http://kieranbroadfoot.com
#

import sys
import os
import telnetlib
import re
import time
from xml.dom.minidom import parseString
from threading import Timer
import StringIO
from time import strftime

class Plugin(indigo.PluginBase):

	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		self.validConnections = False
		self.events = {}
		self.currentTimers = {}
		self.cgateLocation = pluginPrefs.get("cgateNetworkLocation", "127.0.0.1")
		self.cbusNetwork = pluginPrefs.get("cbusNetwork", "254")
		self.cbusSecurityEnabled = pluginPrefs.get("cbusSecurityEnabled", False)
		self.cbusProjectName = ""
		self.cbusLightingMap = {}
		self.cbusSecurityMap = {}
		self.cbusUnitMap = {}
		
		# set up the dispatch table
		self.dispatchTable = {
			"lighting_ramp": self.lightingRamp,
			"lighting_terminateramp": self.lightingTerminateRamp,
			"lighting_on": self.lightingOn,
			"lighting_off": self.lightingOff,
			"security_zone_unsealed": self.zoneUnsealed,
			"security_zone_sealed": self.zoneSealed,
			"security_zone_open": self.zoneOpen,
			"security_zone_short": self.zoneShort,
			"security_zone_isolated": self.zoneIsolated,
			"security_arm_not_ready": self.zoneArmNotReady,
			"security_arm_ready": self.panelArmReady,
			"security_system_arm": self.panelSystemArmed,
			"security_system_disarmed": self.panelSystemDisarmed,
			"security_exit_delay_started": self.panelExitDelay,
			"security_entry_delay_started": self.panelEntryDelay,
			"security_alarm_on": self.panelAlarmOn,
			"security_current_alarm_type": self.panelAlarmType,
			"security_alarm_off": self.panelAlarmOff,
			"security_tamper_on": self.panelTamperOn,
			"security_tamper_off": self.panelTamperOff,
			"security_panic_activated": self.panelPanicActivated,
			"security_panic_cleared": self.panelPanicCleared,
			"security_battery_charging": self.panelBatteryCharging,
			"security_low_battery_detected": self.panelLowBatteryDetected,
			"security_low_battery_corrected": self.panelLowBatteryCorrected,
			"security_mains_failure": self.panelMainsFailure,
			"security_mains_restored": self.panelMainsRestored,
			"security_status_report_1": self.panelStatusReportOne,
			"security_status_report_2": self.panelStatusReportTwo
		}
		
		# set up some mappings from C-Bus to Device states
		self.alarmTypes = {
			"0": "alarmCleared",
			"1": "alarmIntruder",
			"2": "alarmLineCut",
			"3": "alarmFailed",
			"4": "alarmFire",
			"5": "alarmGas"
		}
		self.zoneStates = {
			"0": "monitoring",
			"1": "triggered",
			"3": "open",
			"4": "short"
		}
		self.alarmArmedStates = {
			"0": "disarmed",
			"1": "away",
			"2": "night",
			"3": "day"
		}

	def __del__(self):
		indigo.PluginBase.__del__(self)

	def startup(self):
		self.logger.info("starting c-bus plugin")
		self.fixAlarmZones()
		if self.loadConnections(self.cgateLocation):
			self.getReadyState()

			# refactor to pass application ID and type (e.g. 'lighting')
			self.cbusLightingMap = self.generateGroupData('56','lighting')

			# find unit types in order to map lighiting groups to channel types
			self.generateDeviceTypesPerGroup()

			# map channel types to groups
			self.mapLightingDevices()
			self.createLightingDevices()

			# generate Security devices if needed
			if self.cbusSecurityEnabled:
				self.cbusSecurityMap = self.generateGroupData('208','security')
				self.createSecurityPanel()
				self.createSecurityZones()
				# at this point we have no state for any device. this is determined via a status_request - see concurrent thread

	def shutdown(self):
		self.logger.info("stopping c-bus plugin")
		if self.validConnections:
			self.connection.close()

	def validatePrefsConfigUi(self, valuesDict):
		if self.loadConnections(valuesDict["cgateNetworkLocation"]):
			self.cgateLocation = valuesDict["cgateNetworkLocation"]
			return True
		else:
			errorDict = indigo.Dict()
			errorDict["cgateNetworkLocation"] = "Invalid location"
			return (False, valuesDict, errorDict)

	def validateDeviceConfigUi(self, valuesDict, typeId, devId):
		proposedAddress = self.cbusNetwork+"/p/"+valuesDict["lightSensorAddress"]
		device = None
		for dev in indigo.devices.iter("self"):
			if dev.address == proposedAddress:
				device = indigo.devices[dev.name]
		if device == None:
			valuesDict["address"] = proposedAddress
			valuesDict["SupportsSensorValue"] = True
			valuesDict["sensorValue"] = 0
			return (True, valuesDict)
		else:
			errorDict = indigo.Dict()
			errorDict["lightSensorAddress"] = "Unit already specified in Indigo"
			return (False, valuesDict, errorDict)
		
	def checkboxChanged(self, valuesDict):
		if valuesDict["cbusSecurityEnabled"] == True:
			self.logger.info("enabling c-bus security feature")
			self.cbusSecurityEnabled = True
			if self.cbusSecurityEnabled and self.validConnections:
				self.requestSecurityStatus()
		else:
			self.logger.info("disabling c-bus security feature")
			self.cbusSecurityEnabled = False
		return valuesDict

	def triggerStartProcessing(self, trigger):
		if trigger.pluginTypeId not in self.events:
			self.events[trigger.pluginTypeId] = {trigger.id: trigger}
		else:
			self.events[trigger.pluginTypeId][trigger.id] = trigger

	def triggerStopProcessing(self, trigger):
		if trigger.pluginTypeId in self.events and trigger.id in self.events[trigger.pluginTypeId]:
			del self.events[trigger.pluginTypeId][trigger.id]

	########################################
	# MONITORING
	########################################

	def runConcurrentThread(self):
		self.logger.info("starting c-bus monitoring thread")
		# we have a connection so if security is enabled let's request an initial status
		if self.cbusSecurityEnabled and self.validConnections:
			self.requestSecurityStatus()
		counter = 0
		while self.stopThread == False:
			if self.validConnections:
				try:
					data = self.readUntil(self.monitor, ".*\n")
					# we might receive multiple lines in this data string.
					for line in data.split('\n'):
						if line:
							if line.startswith("# "):
								line = line[2:]
							# split on space and concat 0 and 1. look up in the dispatch table
							action = line.split()
							if len(action) < 2:
								continue
							try:
								lookup = action[0]+"_"+action[1]
								if lookup in self.dispatchTable:
									self.dispatchTable[lookup](action[2:])
							except IndexError:
								self.logger.warn("index error: %s" % (line))
				except Exception:
					self.logger.warn("exception occurred whilst monitoring c-bus")
					pass
				counter = counter + 1
				if counter > 60:
					self.readLightSensors()
					counter = 0
			else:
				# wait a while to see if the user gives us a valid c-gate configuration
				self.sleep(5)
		if self.validConnections:
			self.monitor.close()

	def stopConcurrentThread(self):
		self.stopThread = True

	########################################
	# COMMUNICATION (WITH INDIGO) FUNCTIONS
	########################################

	def valueFromIndigo(self, value):
		return str(int(value * 2.55))

	def valueToIndigo(self, value):
		return int(int(value) / 2.55)

	def updateIndigoLightingState(self, device, state, brightness, source="self"):
		if device:
			if source is not "self":
				# only generate broadcasts when state changes are seen on the c-bus network. it's a sure way to
				# know that changes Indigo generates have taken effect.
				broadcastType = u"lightingStateChanged"
				broadcastPacket = {'deviceName': device.name, 'deviceAddress': device.address, 'type': 'relay', 'state': 'off'}
				if state:
					broadcastPacket['state'] = "on"
				if device.deviceTypeId == "cbusDimmer":
					broadcastPacket['type'] = "dimmer"
					broadcastPacket['brightness'] = self.valueToIndigo(brightness)
				source = source.split("=")[1]
				if self.cbusUnitMap[source]['unit'] == "cbusSwitch":
					# specific behaviours if the request originated from the c-bus network, therefore a manual update
					broadcastType = u"lightingStateManuallyChanged"
					if "groupManuallyChanged" in self.events:
						for trigger in self.events["groupManuallyChanged"]:
							if self.events["groupManuallyChanged"][trigger].pluginProps['group'] == device.address:
								shouldTrigger = False
								if self.events["groupManuallyChanged"][trigger].pluginProps['changeType'] == "any":
									shouldTrigger = True
								if self.events["groupManuallyChanged"][trigger].pluginProps['changeType'] == "on" and brightness == 255:
									shouldTrigger = True
								if self.events["groupManuallyChanged"][trigger].pluginProps['changeType'] == "off" and brightness == 0:
									shouldTrigger = True
								if shouldTrigger:
									indigo.trigger.execute(trigger)
					if "anyGroupManuallyChanged" in self.events:
						for trigger in self.events["anyGroupManuallyChanged"]:
							indigo.trigger.execute(trigger)
				indigo.server.broadcastToSubscribers(broadcastType, broadcastPacket)
			device.updateStateOnServer("onOffState", state)
			if device.deviceTypeId == "cbusDimmer" and brightness:
				device.updateStateOnServer("brightnessLevel", self.valueToIndigo(brightness))

	def updateIndigoSecurityState(self, device, stateType, state):
		if device:
			device.updateStateOnServer(stateType, value=state)
			indigo.server.broadcastToSubscribers(u"securityStateChange", {'deviceName': device.name, 'deviceAddress': device.address, 'state': state})
			
			# HomeKit support
			
			if stateType == "state" and state in ["triggered", "monitoring", "open", "short", "isolated"]:
				if state in ["triggered", "open", "isolated"]:
					device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
					device.updateStateOnServer("onOffState", value=True, uiValue=state)
				else:
					device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
					device.updateStateOnServer("onOffState", value=False, uiValue=state)
			
			# we also want to execute triggers associated to this action.
			# the "state" value is also the name of the trigger
			# if the device is the panel then execute all triggers
			# if the device is a zone then iterate all triggers and find associated type based on device.address
			if state in self.events:
				for trigger in self.events[state]:
					if device.deviceTypeId == "cbusSecurityZone":
						# check for match against specific triggers with referenced zones
						if str(device.id) == str(self.events[state][trigger].pluginProps['device']):
							indigo.trigger.execute(trigger)
					else:
						# must be an alarm panel trigger
						indigo.trigger.execute(trigger)

	def findDevice(self, address):
		# remove //project name and lookup in indigo
		m = re.match("\/\/\w+\/([\w|\/]+).*", address)
		if m:
			address = m.group(1)
			for dev in indigo.devices.iter("self"):
				if dev.address == address:
					return indigo.devices[dev.name]
		return None

	########################################
	# COMMUNICATION (WITH C-BUS) FUNCTIONS
	########################################

	def loadConnections(self, location):
		while True:
			try:
				self.connection = telnetlib.Telnet(location, 20023)
				# querying the physical units (e.g. Light Sensors) occurs asynchronously via concurrent thread
				# to avoid reading the wrong data we need a second channel
				self.thread_connection = telnetlib.Telnet(location, 20023)
				self.monitor = telnetlib.Telnet(location, 20025)
				self.readUntil(self.connection, "201 Service ready:.*")
				self.readUntil(self.thread_connection, "201 Service ready:.*")
				self.validConnections = True
				self.logger.info("connected to C-Gate")
				return True
			except Exception:
				self.logger.warn("unable to connect to C-Gate. waiting 10 seconds for retry")
				self.sleep(10)

	def getReadyState(self):
		ready = False
		while ready != True:
			self.writeTo(self.connection, "net list\r\n")
			networkState = self.readUntil(self.connection, "131.*")
			check = re.match(".*network="+self.cbusNetwork+" State=ok.*",networkState)
			if check:
				self.logger.info("c-bus network ready")
				ready = True
			else:
				self.logger.warn("c-bus network not yet ready. waiting 10 seconds for retry")
				self.sleep(10)

	def requestSecurityStatus(self):
		self.logger.info("requesting initial security status")
		# request 1 represents zones up to 32, 2 provides 33-80
		for request in ['1','2']:
			self.writeTo(self.connection, "security status_request "+self.cbusNetwork+"/208 "+request+"\r\n")
			self.readUntil(self.connection, "200 OK:.*")

	def readUntil(self, connection, str, timeout=1):
		try:
			return connection.read_until(str, timeout)
		except EOFError:
			self.logger.warn("lost connection to c-gate. attempting to reconnect")
			if self.loadConnections(self.cgateLocation):
				self.getReadyState()
				return ""

	def writeTo(self, connection, str):
		connection.write(str.encode('latin-1'))

	def rampChannel(self, device, actionString, level, timer=0):
		self.writeTo(self.connection,"ramp "+self.cbusNetwork+"/56/"+device.pluginProps['unqualifiedAddress']+" "+level+" "+str(timer)+"s\r\n")
		result = self.readUntil(self.connection, "200 OK:.*")
		if result == '':
			self.logger.warn("send \"%s\" %s to %d failed" % (device.name, actionString, int(level)))
		else:
			if timer > 0:
				self.logger.info("sent \"%s\" %s to %d over %d seconds" % (device.name, actionString, int(level), timer))
			else:
				self.logger.info("sent \"%s\" %s to %d" % (device.name, actionString, int(level)))
			if int(level) > 0:
				self.updateIndigoLightingState(device, True, level)
			else:
				self.updateIndigoLightingState(device, False, level)
				device.updateStateOnServer("onOffState", False)

	def readLightSensors(self):
		device = None
		for dev in indigo.devices.iter("self.cbusLightSensor"):
			self.writeTo(self.thread_connection, "get "+dev.address+" LightLevel\r\n")
			result = self.readUntil(self.thread_connection, "300.*")
			level_split = result.split("=")
			if len(level_split) > 1:
				keyValueList = []
				keyValueList.append({'key':'sensorValue', 'value':int(level_split[-1])})
				dev.updateStatesOnServer(keyValueList)
			else:
				self.logger.error(dev.address+" does not appear to be a Light Sensor")

	########################################
	# INITIALISATION FUNCTIONS
	########################################
	
	def fixAlarmZones(self):
		# Check if Alarm Zones have onState property.  If not, recast them.  Introduced change in July 18 to move from custom to sensor base object
		for dev in indigo.devices.iter("self.cbusSecurityZone"):
			try:
				if dev.supportsOnState:
					continue
			except Exception:
				self.logger.info("updating alarm zone: "+dev.name)
				dev = indigo.device.changeDeviceTypeId(dev, "cbusSecurityZone")
				dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
				dev.updateStateOnServer("onOffState", value=False, uiValue="monitoring")

	def generateGroupData(self, appId, groupType):
		self.logger.info("searching for c-bus %s groups" % (groupType))
		while True:
			# use dbgetxml 254/appId to determine names/OID/address of each group
			self.writeTo(self.connection, "dbgetxml "+self.cbusNetwork+"/"+appId+"\r\n")
			xml = self.readUntil(self.connection, "344 End XML snippet")
			xml = xml.replace("343-Begin XML snippet","")
			xml = xml.replace("347-","",2)
			xml = xml.replace("344 End XML snippet","")
			xml = xml.replace("\n","",5)
			xml = xml.replace("<?xml version=\"1.0\" encoding=\"utf-8\"?>","")
			try:
				mapping = {}
				dom = parseString(xml)
				for group in dom.getElementsByTagName("Group"):
					address = group.getElementsByTagName("Address")[0].childNodes[0].data
					mapping[self.cbusNetwork+"/"+appId+"/"+address] = {'oid':group.getElementsByTagName("OID")[0].childNodes[0].data,
											'name':group.getElementsByTagName("TagName")[0].childNodes[0].data,
											'unqualifiedAddress':address, 'level':'0'}
				return mapping
			except Exception:
				self.logger.warn("c-bus %s database not yet ready. waiting 10 seconds for retry" % (groupType))
				self.sleep(10)

	def generateDeviceTypesPerGroup(self):
		self.logger.info("searching for c-bus units")
		self.writeTo(self.connection, "tree "+self.cbusNetwork+"\r\n")
		treeStr = self.readUntil(self.connection, "320 -end-")
		# we are looking for two types of items.
		# Units (to determine which objects are relays and dimmers)
		# Groups (so we can match them to correct unit type)
		for line in StringIO.StringIO(treeStr):
			line = line.rstrip()
			m0 = re.match(".*(\/\/\w+)\/.*p\/(\w+).*type\=(\w+).*groups\=(.*)", line)
			m1 = re.match(".*\/56\/(\w+).*level\=(\w+).*units\=(.*)", line)
			if m0:
				# Capture the name of the C-Bus project for later use when DLT Labelling
				self.cbusProjectName = m0.group(1)
				unitType = "unknown"
				if re.match("DIM.*",m0.group(3)):
					unitType = "cbusDimmer"
				if re.match("REL.*",m0.group(3)):
					unitType = "cbusRelay"
				if re.match("KEY.*",m0.group(3)):
					unitType = "cbusSwitch"
				self.cbusUnitMap[m0.group(2)] = { 'unit':unitType, 'groups': m0.group(4).split(',') }
			elif m1:
				if self.cbusNetwork+"/56/"+m1.group(1) in self.cbusLightingMap:
					self.cbusLightingMap[self.cbusNetwork+"/56/"+m1.group(1)]['level'] = m1.group(2)
					self.cbusLightingMap[self.cbusNetwork+"/56/"+m1.group(1)]['units'] = m1.group(3).split(',')

	def mapLightingDevices(self):
		self.logger.info("mapping c-bus lighting groups to channel types")
		# for each item in lighting map, find the units that supports it.  if not found set to relay
		# if found on multiple units then default to relay
		# then create the device
		for group in self.cbusLightingMap.keys():
			for unit in self.cbusUnitMap:
				if self.cbusUnitMap[unit]['unit'] == "unknown" or self.cbusUnitMap[unit]['unit'] == "cbusSwitch":
					# ignore devices in the c-bus network which have not been mapped or are switches.  We use switches for events in updateIndigoLightingState
					continue
				if self.cbusLightingMap[group]['unqualifiedAddress'] in self.cbusUnitMap[unit]['groups']:
					# we need to account for all unit groups
					if 'type' not in self.cbusLightingMap[group]:
						self.cbusLightingMap[group]['type'] = self.cbusUnitMap[unit]['unit']
					else:
						# we've already matched before.	 if unit is relay and value is dimmer replace, otherwise do nothing
						# if a group applies across multiple unit types then we need to apply the lowest common feature set to it
						# for all unit groups that is typically on/off
						if self.cbusLightingMap[group]['type'] == "dimmer" and self.cbusUnitMap[unit]['unit'] == "relay":
							self.cbusLightingMap[group]['type'] = self.cbusUnitMap[unit]['unit']

			# if we havent seen a match then really we should set to relay type however for those of us using
			# MRA like functionality (e.g. audio controls etc) then dimming functionality is required.	Should
			# make this a configurable option?
			if 'type' not in self.cbusLightingMap[group]:
				self.cbusLightingMap[group]['type'] = "cbusDimmer"

	def createLightingDevices(self):
		self.logger.info("creating c-bus lighting devices in Indigo")
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
			
			onState = True
			if int(self.cbusLightingMap[group]['level']) == 0:
				onState = False
			if self.cbusLightingMap[group]['type'] == "cbusDimmer":
				self.updateIndigoLightingState(device, onState,self. cbusLightingMap[group]['level'])
			else:
				self.updateIndigoLightingState(device, onState, None)

	def createSecurityPanel(self):
		self.logger.info("creating c-bus security panel device in Indigo")
		# I currently presume there is only one c-bus enabled alarm panel.
		panel = None
		for dev in indigo.devices.iter("self.cbusSecurityAlarmPanel"):
			if dev.address == "254/208":
				panel = indigo.devices[dev.name]
		if panel == None:
			panel = indigo.device.create(protocol=indigo.kProtocol.Plugin,
				address="254/208",
				name="Alarm Panel",
				description="C-Bus Enabled Alarm Panel",
				pluginId="uk.co.l1fe.indigoplugin.C-Bus",
				deviceTypeId="cbusSecurityAlarmPanel")
			self.updateIndigoSecurityState(panel, "mainsState", "ok")
			self.updateIndigoSecurityState(panel, "batteryState", "ok")

	def createSecurityZones(self):
		self.logger.info("creating c-bus security zones in Indigo")
		for group in self.cbusSecurityMap.keys():
			device = None
			for dev in indigo.devices.iter("self.cbusSecurityZone"):
				if dev.address == group:
					device = indigo.devices[dev.name]
			if device == None:
				device = indigo.device.create(protocol=indigo.kProtocol.Plugin,
					address=group,
					name=self.cbusSecurityMap[group]['name'],
					description=self.cbusSecurityMap[group]['name'],
					pluginId="uk.co.l1fe.indigoplugin.C-Bus",
					deviceTypeId="cbusSecurityZone")
				self.updateIndigoSecurityState(device, "state", "monitoring")

	########################################
	# MONITORING DISPATCH FUNCTIONS
	########################################

	def lightingRampTimerCallback(self, device, brightness, sourceunit):
		del self.currentTimers[device]
		self.updateIndigoLightingState(self.findDevice(device), True, brightness, sourceunit)

	def lightingRamp(self, action):
		# updated to account for ramping behaviour.	 when a user initiates a ramp c-bus will send a timed ramp
		# message of 0 or 255 over X seconds. If the user releases their finger then an immediate ramp to level
		# message is sent.	We'll create a timer for the initial press and cancel if the user removes their finger
		# before the timer completes.  If the timer completes then the user has ramped to 1 or 255 manually.
		if int(action[2]) > 0:
			self.currentTimers[action[0]] = Timer(int(action[2]), self.lightingRampTimerCallback, [action[0], action[1], action[3]])
			self.currentTimers[action[0]].start()
		else:
			if action[0] in self.currentTimers:
				self.currentTimers[action[0]].cancel()
				del self.currentTimers[action[0]]
			self.updateIndigoLightingState(self.findDevice(action[0]), True, action[1], action[3])

	def lightingTerminateRamp(self, action):
		level = action[1].split("=")[1]
		if level == "0":
			self.updateIndigoLightingState(self.findDevice(action[0]), False, 0, action[2])
		else:
			self.updateIndigoLightingState(self.findDevice(action[0]), True, level, action[2])

	def lightingOn(self, action):
		self.updateIndigoLightingState(self.findDevice(action[0]), True, 255, action[1])

	def lightingOff(self, action):
		self.updateIndigoLightingState(self.findDevice(action[0]), False, 0, action[1])

	def zoneUnsealed(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "triggered")

	def zoneSealed(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "monitoring")

	def zoneOpen(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "open")

	def zoneShort(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "short")

	def zoneIsolated(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "isolated")

	def zoneArmNotReady(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "notReady")

	def panelArmReady(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "armReady")

	def panelSystemArmed(self, action):
		if action[1] in self.alarmArmedStates:
			self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "armed")
			self.updateIndigoSecurityState(self.findDevice(action[0]), "state", self.alarmArmedStates[action[1]])

	def panelSystemDisarmed(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "disarmed")

	def panelExitDelay(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "exitDelay")

	def panelEntryDelay(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "entryDelay")

	def panelAlarmOn(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "alarmActivated")

	def panelAlarmType(self, action):
		# as per: http://www3.clipsal.com/cis/downloads/Toolkit/CGateServerGuide_1_0.pdf
		# 1 = intruder, 2 = line cut, 3 = arm failed, 4 = fire, 5 = gas
		# we ignore all other types at this time.  We would already have raised a generic alarm
		if action[1] in self.alarmTypes:
			self.updateIndigoSecurityState(self.findDevice(action[0]), "state", self.alarmTypes[action[1]])

	def panelAlarmOff(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "alarmDisabled")
		# Once we have cleared the current alarm let's re-sync back to the state of the panel
		self.requestSecurityStatus()

	def panelTamperOn(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "alarmTamperActivated")

	def panelTamperOff(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "alarmTamperCleared")

	def panelPanicActivated(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "panicActivated")

	def panelPanicCleared(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "panicCleared")

	def panelBatteryCharging(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "batteryState", "charging")

	def panelLowBatteryDetected(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "batteryState", "low")

	def panelLowBatteryCorrected(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "batteryState", "batteryOK")

	def panelMainsFailure(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "mainsState", "failure")

	def panelMainsRestored(self, action):
		self.updateIndigoSecurityState(self.findDevice(action[0]), "mainsState", "mainsOK")

	# in status report 1 the first value is the alarm state 0 = disarmed
	# second value = tamper state
	# third value = panic state
	# all other values represet the state of each zone.	 0 = sealed, 1 = unsealed, 3 = open, 4 = short
	def panelStatusReportOne(self, action):
		if action[1] in self.alarmArmedStates:
			self.updateIndigoSecurityState(self.findDevice(action[0]), "state", self.alarmArmedStates[action[1]])
		if action[2] == "1":
			self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "alarmTamperActivated")
		if action[3] == "1":
			self.updateIndigoSecurityState(self.findDevice(action[0]), "state", "panicActivated")
		for index, value in enumerate(action[3:]):
			zone = self.findDevice(self.cbusNetwork+"/208/"+str(index+1))
			if zone and value in self.zoneStates:
				self.updateIndigoSecurityState(zone, "state", self.zoneStates[value])

	# all values in status report 2 represent zones 33 through 80
	def panelStatusReportTwo(self, action):
		for index, value in enumerate(action[1:]):
			zone = self.findDevice(self.cbusNetwork+"/208/"+str(index+33))
			if zone and value in self.zoneStates:
				self.updateIndigoSecurityState(zone, "state", self.zoneStates[value])

	########################################
	# ACTION CALLBACKS
	########################################

	def actionControlDimmerRelay(self, action, dev):
		###### TURN ON ######
		if action.deviceAction == indigo.kDeviceAction.TurnOn:
			# Command hardware module (dev) to turn ON here:
			# Homekit sends a brightness request followed by a request to turn on.  
			# Given "turn on" means 100% you get some odd behaviour as the levels bounce between requested dim level and max.  
			# Reloading the device state ensures the device gives us the absolute latest status following rampChannel updating
			# the device.  We now ignore if onState isT rue irrelevant of level.  This might break some use-cases, e.g. using
			# turn On to ramp to max if the channel is already at a designated ramp level
			latest_dev = indigo.devices[dev.id]
			if latest_dev.onState == False:
				self.writeTo(self.connection,"on "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
				result = self.readUntil(self.connection, "200 OK:.*")
				if result == '':
					self.logger.warn("send \"%s\" %s failed" % (dev.name, "on"))
				else:
					self.logger.info("sent \"%s\" %s" % (dev.name, "on"))
					self.updateIndigoLightingState(dev, True, None)
			else:
				self.logger.info("\"%s\" already on" % (dev.name))

		###### TURN OFF ######
		elif action.deviceAction == indigo.kDeviceAction.TurnOff:
			# Command hardware module (dev) to turn OFF here:
			
			self.writeTo(self.connection,"off "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
			result = self.readUntil(self.connection, "200 OK:.*")
			if result == '':
				self.logger.warn("send \"%s\" %s failed" % (dev.name, "off"))
			else:
				self.logger.info("sent \"%s\" %s" % (dev.name, "off"))
				self.updateIndigoLightingState(dev, False, None)

		###### TOGGLE ######
		elif action.deviceAction == indigo.kDeviceAction.Toggle:
			# Command hardware module (dev) to toggle here:
			newOnState = not dev.onState
			
			if newOnState:
				self.writeTo(self.connection,"on "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
			else:
				self.writeTo(self.connection,"off "+self.cbusNetwork+"/56/"+dev.pluginProps['unqualifiedAddress']+"\r\n")
			result = self.readUntil(self.connection, "200 OK:.*")
			
			if result == '':
				self.logger.warn("send \"%s\" %s failed" % (dev.name, "toggle"))
			else:
				self.logger.info("sent \"%s\" %s" % (dev.name, "toggle"))
				self.updateIndigoLightingState(dev, newOnState, None)

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
			self.logger.info(u"sent \"%s\" %s" % (dev.name, "status request"))

	########################################
	# ACTION CALLBACKS
	########################################

	def rampGroupWithTimer(self, action, dev):
		if not action.props.get("cbusGroup","") or not action.props.get("numberOfSeconds","") or not action.props.get("level"):
			self.logger.warn("timed ramp: no c-bus group, timer or level provided.")
		else:
			try:
				for dev in indigo.devices.iter("self"):
					if dev.address == action.props.get("cbusGroup",""):
						self.rampChannel(dev, "ramp", self.valueFromIndigo(int(action.props.get("level",""))), int(action.props.get("numberOfSeconds")))
			except TypeError:
				self.logger.warn("timed ramp: level or timer not a valid integer")

	def terminateRampOnGroup(self, action, dev):
		if not action.props.get("cbusGroup",""):
			self.logger.warn("terminate ramp: No c-bus group provided.")
		else:
			for dev in indigo.devices.iter("self"):
				if dev.address == action.props.get("cbusGroup",""):
					self.logger.info("terminate ramp \"%s\"" % (dev.name))
					self.writeTo(self.connection,"terminateramp "+action.props.get("cbusGroup","")+"\r\n")

	def updateDLTLabel(self, action, dev):
		if not action.props.get("cbusGroup",""):
			self.logger.warn("dlt label: no c-bus group provided.")
		else:
			# Get name of the group for logging purposes
			devAddr = self.cbusNetwork+"/56"+action.props.get("cbusGroup","")
			devName = ""
			for dev in indigo.devices.iter("self"):
				if dev.address == devAddr:
					devName = dev.name
			if not action.props.get("dltLabel",""):
				self.logger.warn("dlt label: no label provided.")
			else:
				# before further checks lets resolve the potentially templated label.
				label = self.generateLabel(action.props.get("dltLabel",""))
				if len(label) > 9:
					self.logger.warn("dlt label is too long (<=9 chars): \"%s\" \"%s\"" % (devName, label))
				if action.props.get("cbusGroup","") and label and len(label) <= 9:
					self.logger.info("dlt label update \"%s\" \"%s\"" % (devName, label))
					self.writeTo(self.connection, "lighting label "+self.cbusProjectName+"/"+self.cbusNetwork+"/56 1 "+ action.props.get("cbusGroup","").split("/")[2] +" - 0 "+label.encode("hex")+"\r\n")

	def cbusGroupList(self, filter="", valuesDict=None, typeId="", targetId=0):
		# used by DLT labelling action.
		groups = []
		for group in self.cbusLightingMap.keys():
			groups.append([group, self.cbusLightingMap[group]['name']])
		return sorted(groups, key=lambda x: x[1])

	def sendTime(self, action, dev):
		self.logger.info("updating c-bus time")
		self.writeTo(self.connection, "clock time "+self.cbusNetwork+"/223 "+strftime("%H:%M:%S")+"\r\n")

	def sendDate(self, action, dev):
		self.logger.info("updating c-bus date")
		self.writeTo(self.connection, "clock date "+self.cbusNetwork+"/223 "+strftime("%Y-%m-%d")+"\r\n")

	########################################
	# MISC FUNCTIONS
	########################################

	def generateLabel(self, text):
		# a very simple templating engine to extract IOM expressions
		potential = False
		evaluate = False
		result = ""
		evalstr = ""
		for char in text:
			if char == "$":
				potential = True
			elif char == "{" and potential:
				evaluate = True
			elif char == "}":
				if evaluate:
					result = result + str(eval(evalstr))
					evalstr = ""
					potential = False
					evaluate = False
				else:
					# found } but not in eval state
					result = result + char
			else:
				if evaluate:
					evalstr = evalstr+char
				else:
					result = result + char
					potential = False
		return result
