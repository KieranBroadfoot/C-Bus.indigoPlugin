C-Bus.indigoPlugin
==================

A [Clipsal C-Bus](http://www.clipsal.com/consumer/products/smart_home_technology/c-bus_home_control) plugin for [Indigo Domotics Indigo 7](http://www.indigodomo.com)

Lighting
--------

This plugin supports simple operations for application 56:

* lighting on
* lighting off
* lighting ramp
* DLT labelling
* lighting ramp (over X seconds)
* terminate ramp
* update time/date

The plugin dynamically generates devices for each lighting group and will attempt to determine if the group is associated to a dimmer or relay channel.  If it cannot guess (which means it cannot determine the unit type which is supporting the group) it will default to a dimmer channel.  

A single "Group Manually Changed" trigger is available for application 56 which enables you to monitor for human initiated group changes from a switch (e.g. DLT).  You can monitor for on/off/any changes.

Finally, there is a *very* simple templating engine in the plugin to interpolate indigo state into your DLT labels.  Simply wrap your python expression in ${}.  An example DLT Label might be: "Temp: ${indigo.devices["Bathroom"].states["temperatureInput1"]}C"

Security
--------

As of version 0.0.2 this plugin also provides support for the security application (208).  An alarm panel which supports this specification will be observed by the plugin and associated devices created in Indigo.  Security support should be enabled via the plugin configuration.

It should be noted that the C-Bus Toolkit must be used to create groups under application 208 for each zone associated to the alarm.  Use File -> Preferences -> Compatibility to enable legacy applications.  Once this has been enabled simply add the application to your project and create your groups.  This activity must be undertaken for the plugin to find and create your devices.  The Group Name specified in the Toolkit will be used to name your zones.

The plugin is READ-ONLY and therefore cannot be used to arm/disarm.

The security feature of this plugin supports events for every device state listed below so you can choose to react to events or device state changes depending on your preference.

### Panel Device State

The primary panel state provides the following states.  According to the specification an alarm signal should be sent on the C-Bus network before a specific alarm type message is sent.  As such a compliant alarm panel should notify Indigo of an alarm state and then inform it of a specific alarm type.  General behaviours could be triggered against the Alarm! state and specific activities executed based on the type of alarm.

* Armed (Away)
* Armed (Day)
* Armed (Night)					
* Ready to Arm
* Armed
* Disarmed
* Exit Delay Started
* Entry Delay Started
* Alarm!
* Intruder Alarm!
* Line Cut Alarm!
* Arm Failed!
* Fire Alarm!
* Gas Alarm!
* Alarm Cleared
* Tamper Alarm!
* Tamper Cleared
* Panic Activated
* Panic Cleared

### Panel Battery State

If the panel fully supports the specification the plugin may also be able to inform Indigo about battery state changes

* OK
* Charging
* Low Battery

### Panel Mains State

A compliant panel should also be able to inform Indigo about mains power state changes too

* OK
* Mains Failed

### Zone Device State

Each zone also has a single state which can be any one of the following conditions:

* Triggered
* Monitoring
* Open
* Short
* Isolated
* Arm Not Ready

The final condition indicates that the alarm panel attempted to arm itself but a zone was not ready to arm.  Reacting to triggered events or state changes is the most likely use-case.

Broadcast Messages
------------------

The plugin (as of v1.0) generates Indigo 7 broadcast messages for use by other plugins.  

```
PluginID: uk.co.l1fe.indigoplugin.C-Bus
MessageType: lightingStateChanged | lightingStateManuallyChanged 
Returns dictionary:
 {
    'deviceName':  <text string>,
    'deviceAddress': <text string>,
    'state': "on|off",
    'type': "relay|dimmer",
    'brightness': <integer>
}
MessageType: securityStateChange 
Returns dictionary:
 {
    'deviceName':  <text string>,
    'deviceAddress': <text string>,
    'state': <text string>
}
```

C-Gate Setup
------------

The plugin presumes you have an operating C-Gate installation on your local network.  C-Gate is a java application and whilst it primarily runs on Windows it can also be moved to a Linux device.  Wherever you choose to run the C-Gate server it is essential you update the C-GateConfig.txt file to specify your project.default and project.start values.  This automatically enables your project when C-Gate is started.

Known C-Bus Enabled Panels
--------------------------

This plugin has been tested with the [Cytech Comfort](http://www.cytech.biz) alarm panel.

