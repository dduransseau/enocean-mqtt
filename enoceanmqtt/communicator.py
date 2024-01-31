# Copyright (c) 2020 embyt GmbH. See LICENSE for further details.
# Author: Roman Morawek <roman.morawek@embyt.com>
"""this class handles the enocean and mqtt interfaces"""
import logging
import queue
import numbers
import json
import platform

from enocean.communicators.serialcommunicator import SerialCommunicator
from enocean.protocol.packet import RadioPacket
from enocean.equipment import Equipment as EnoceanEquipment
from enocean.protocol.constants import PACKET, RETURN_CODE, RORG
import enocean.utils
import paho.mqtt.client as mqtt


class Equipment(EnoceanEquipment):

    logger = logging.getLogger('enocean.mqtt.equipment')

    def __init__(self, **kwargs):
        address = kwargs["address"]
        rorg = int(kwargs.get("rorg"))
        func = int(kwargs.get("func"))
        type_ = int(kwargs.get("type"))
        name = kwargs.get("name")
        topic_prefix = kwargs.get("topic_prefix")
        if topic_prefix and name.startswith(topic_prefix):
            name = name.replace(topic_prefix, "")
        # self.logger.debug(f"Lookup profile for {rorg} {func} {type_}")
        super().__init__(address=address, rorg=rorg, func=func, type_=type_, name=name)
        self.publish_json = True if kwargs.get("publish_json", False) in ("true", "True", "1") else False
        self.publish_rssi = True if kwargs.get("publish_rssi", False) in ("true", "True", "1") else False
        self.publish_date = True if kwargs.get("publish_date", False) in ("true", "True", "1") else False
        self.retain = True if kwargs.get("persistent", False) in ("true", "True", "1") else False
        self.log_learn = True if kwargs.get("log_learn", False) in ("true", "True", "1") else False
        self.ignore = True if kwargs.get("ignore", False) in ("true", "True", "1") else False
        self.answer = kwargs.get("answer")
        self.command = kwargs.get("command")
        self.channel = kwargs.get("channel")
        self.direction = kwargs.get("direction")
        self.sender = kwargs.get("sender")
        self.default_data = kwargs.get("default_data")
        self.data = dict()
        # Allow to specify a topic different from name to allow blank
        if topic := kwargs.get("topic"):
            self.topic = f"{topic_prefix}{topic}"
        else:
            self.topic = f"{topic_prefix}{name}"
        # self.logger.debug(f"Received kwargs {kwargs}")

    @property
    def definition(self):
        return dict(
            eep=self.eep_code,
            description=self.description,
            address=enocean.utils.to_hex_string(self.address),
            direction=self.direction,
            topic=self.topic,
            config=dict(
                publish_json=self.publish_json,
                publish_rssi=self.publish_rssi,
                publish_date=self.publish_date,
                retain=self.retain,
                ignore=self.ignore,
                command=self.command,
                sender=self.sender,
                answer=self.answer
            )
        )


# logging.basicConfig(level=logging.DEBUG)
class Communicator:
    """the main working class providing the MQTT interface to the enocean packet classes"""
    mqtt = None
    enocean = None

    logger = logging.getLogger('enocean.mqtt.communicator')

    CONNECTION_RETURN_CODE = [
        "connection successful",
        "incorrect protocol version",
        "invalid client identifier",
        "server unavailable",
        "bad username or password",
        "not authorised",
    ]

    def __init__(self, config, sensors):
        self.conf = config
        # self.sensors = sensors
        self.logger.info(f"Init communicator with sensors: {sensors}")
        if topic_prefix := self.conf.get("mqtt_prefix"):
            if not topic_prefix.endswith("/"):
                topic_prefix = f"{topic_prefix}/"
        else:
            topic_prefix = ""
        self.topic_prefix = topic_prefix
        self.equipments = self.setup_devices_list(topic_prefix, sensors)

        # check for mandatory configuration
        if 'mqtt_host' not in self.conf or 'enocean_port' not in self.conf:
            raise Exception("Mandatory configuration not found: mqtt_host/enocean_port")
        mqtt_port = int(self.conf['mqtt_port']) if 'mqtt_port' in self.conf else 1883
        mqtt_keepalive = int(self.conf['mqtt_keepalive']) if 'mqtt_keepalive' in self.conf else 60

        # setup mqtt connection
        client_id = self.conf['mqtt_client_id'] if 'mqtt_client_id' in self.conf else ''
        self.mqtt = mqtt.Client(client_id=client_id)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_disconnect = self._on_disconnect
        self.mqtt.on_message = self._on_mqtt_message
        self.mqtt.on_publish = self._on_mqtt_publish
        if 'mqtt_user' in self.conf:
            logging.info("Authenticating: %s", self.conf['mqtt_user'])
            self.mqtt.username_pw_set(self.conf['mqtt_user'], self.conf['mqtt_pwd'])
        if str(self.conf.get('mqtt_ssl')) in ("True", "true", "1"):
            logging.info("Enabling SSL")
            ca_certs = self.conf['mqtt_ssl_ca_certs'] if 'mqtt_ssl_ca_certs' in self.conf else None
            certfile = self.conf['mqtt_ssl_certfile'] if 'mqtt_ssl_certfile' in self.conf else None
            keyfile = self.conf['mqtt_ssl_keyfile'] if 'mqtt_ssl_keyfile' in self.conf else None
            self.mqtt.tls_set(ca_certs=ca_certs, certfile=certfile, keyfile=keyfile)
            if str(self.conf.get('mqtt_ssl_insecure')) in ("True", "true", "1"):
                logging.warning("Disabling SSL certificate verification")
                self.mqtt.tls_insecure_set(True)
        if str(self.conf.get('mqtt_debug')) in ("True", "true", "1"):
            self.mqtt.enable_logger()
        logging.debug("Connecting to host %s, port %s, keepalive %s",
                      self.conf['mqtt_host'], mqtt_port, mqtt_keepalive)
        self.mqtt.connect_async(self.conf['mqtt_host'], port=mqtt_port, keepalive=mqtt_keepalive)
        self.mqtt.loop_start()

        # setup enocean communication
        self.enocean = SerialCommunicator(self.conf['enocean_port'])
        self.enocean.start()
        # sender will be automatically determined
        self.enocean_sender = None

    def __del__(self):
        if self.enocean is not None and self.enocean.is_alive():
            self.enocean.stop()

    def setup_devices_list(self, topic_prefix, sensors):
        equipments_list = dict()
        for s in sensors:
            address = s.get("address")
            s["topic_prefix"] = topic_prefix
            # rorg = int(s.get("rorg"))
            # func = int(s.get("func"))
            # type_ = int(s.get("type"))
            # profile = RadioPacket.eep.get_eep(rorg, func, type_)
            # device_list[address] = dict(profile=profile, rorg=rorg, func=func, type=type_, description=profile.description, address=address)
            # self.logger.debug(f"Found profile {profile} for sensor {address}")
            equipment = Equipment(**s)
            equipments_list[address] = equipment
        return equipments_list

    #=============================================================================================
    # MQTT CLIENT
    #=============================================================================================
    def _on_connect(self, mqtt_client, _userdata, _flags, return_code):
        '''callback for when the client receives a CONNACK response from the MQTT server.'''
        if return_code == 0:
            self.logger.info("Succesfully connected to MQTT broker.")
            equipments_definition_list = list()
            # listen to enocean send requests
            for equipment in self.equipments.values():
                # logging.debug("MQTT subscribing: %s", cur_sensor['name']+'/req/#')
                mqtt_client.subscribe(equipment.topic+'/req/#')
                equipments_definition_list.append(equipment.definition)
            self.mqtt.publish(f"{self.topic_prefix}gateway/equipments", json.dumps(equipments_definition_list), retain=True)
        else:
            self.logger.error("Error connecting to MQTT broker: %s",
                          self.CONNECTION_RETURN_CODE[return_code]
                          if return_code < len(self.CONNECTION_RETURN_CODE) else return_code)

    def _on_disconnect(self, _mqtt_client, _userdata, return_code):
        '''callback for when the client disconnects from the MQTT server.'''
        if return_code == 0:
            self.logger.warning("Successfully disconnected from MQTT broker")
        else:
            self.logger.warning("Unexpectedly disconnected from MQTT broker: %s",
                            self.CONNECTION_RETURN_CODE[return_code]
                            if return_code < len(self.CONNECTION_RETURN_CODE) else return_code)

    def _on_mqtt_message(self, _mqtt_client, _userdata, msg):
        '''the callback for when a PUBLISH message is received from the MQTT server.'''
        # search for sensor
        found_topic = False
        self.logger.debug("Got MQTT message: %s", msg.topic)

        # Get how to handle MQTT message
        try:
            try:
                mqtt_payload = json.loads(msg.payload)
            except json.decoder.JSONDecodeError:
                mqtt_payload = msg.payload

            if isinstance(mqtt_payload, dict):
                found_topic = self._mqtt_message_json(msg.topic, mqtt_payload)
            else:
                found_topic = self._mqtt_message_normal(msg)

            if not found_topic:
                self.logger.warning("Unexpected or erroneous MQTT message: %s: %s", msg.topic, msg.payload)
        except Exception:
            self.logger.error(f"Unable to send {msg}")

    def _on_mqtt_publish(self, _mqtt_client, _userdata, _mid):
        '''the callback for when a PUBLISH message is successfully sent to the MQTT server.'''
        #logging.debug("Published MQTT message "+str(mid))


    #=============================================================================================
    # MQTT TO ENOCEAN
    #=============================================================================================

    def get_equipment_by_topic(self, topic):
        for equipment in self.equipments.values():
            if f"{equipment.topic}/" in topic:
                return equipment

    def _mqtt_message_normal(self, msg):
        '''Handle received PUBLISH message from the MQTT server as a normal payload.'''
        found_topic = False
        if equipment := self.get_equipment_by_topic(msg.topic):
            self.logger.debug(f"Received message on {equipment.topic}")
            # get message topic
            prop = msg.topic[len(f"{equipment.topic}/req/"):]
            # do we face a send request?
            if prop == "send":
                found_topic = True

                # Clear sent data, if requested by the send message
                # MQTT payload is binary data, thus we need to decode it
                clear = False
                if msg.payload.decode('UTF-8') == "clear":
                    clear = True

                self._send_message(equipment, clear)

            else:
                found_topic = True
                # parse message content
                value = None
                try:
                    value = int(msg.payload)
                except ValueError:
                    self.logger.warning("Cannot parse int value for %s: %s", msg.topic, msg.payload)
                    # Prevent storing undefined value, as it will trigger exception in EnOcean library
                    return
                # store received data
                self.logger.debug("%s: %s=%s", equipment.name, prop, value)
                equipment.data[prop] = value

        return found_topic

    def _mqtt_message_json(self, mqtt_topic, mqtt_json_payload):
        '''Handle received PUBLISH message from the MQTT server as a JSON payload.'''
        found_topic = False
        if equipment := self.get_equipment_by_topic(mqtt_topic):
            # get message topic
            prop = mqtt_topic[len(f"{equipment.topic}/"):]
            # JSON payload shall be sent to '/req' topic
            if prop == "req":
                found_topic = True
                send = False
                clear = False

                # do we face a send request?
                if "send" in mqtt_json_payload.keys():
                    send = True
                    # Check whether the data buffer shall be cleared
                    if mqtt_json_payload['send'] == "clear":
                        clear = True

                    # Remove 'send' field as it is not part of EnOcean data
                    del mqtt_json_payload['send']

                # Parse message content
                for topic in mqtt_json_payload:
                    try:
                        mqtt_json_payload[topic] = int(mqtt_json_payload[topic])
                    except ValueError:
                        self.logger.warning("Cannot parse int value for %s: %s", topic, mqtt_json_payload[topic])
                        # Prevent storing undefined value, as it will trigger exception in EnOcean library
                        del mqtt_json_payload[topic]

                # Append received data to cur_sensor['data'].
                # This will keep the possibility to pass single topic/payload as done with
                # normal payload, even if JSON provides the ability to pass all topic/payload
                # in a single MQTT message.
                logging.debug("%s: %s=%s", equipment.name, prop, mqtt_json_payload)
                equipment.data.update(mqtt_json_payload)

                # Finally, send the message
                if send == True:
                    self._send_message(equipment, clear)

        return found_topic

    def _send_message(self, sensor, clear):
        '''Send received MQTT message to EnOcean.'''
        self.logger.debug("Trigger message to: %s", sensor.name)
        destination = [(sensor.address >> i*8) &
                       0xff for i in reversed(range(4))]
        self.logger.debug(f"Message {sensor.data} to send to {destination}")
        # Retrieve command from MQTT message and pass it to _send_packet()
        command = None
        command_shortcut = sensor.command

        if command_shortcut:
            # Check MQTT message has valid data
            if not sensor.data:
                self.logger.warning('No data to send from MQTT message!')
                return
            # Check MQTT message sets the command field
            if command_shortcut not in sensor.data or sensor.data[command_shortcut] is None:
                self.logger.warning(
                    'Command field %s must be set in MQTT message!', command_shortcut)
                return
            # Retrieve command id from MQTT message
            command = sensor.data[command_shortcut]
            self.logger.debug('Retrieved command id from MQTT message: %s', hex(command))

        self._send_packet(sensor, destination, command)

        # Clear sent data, if requested by the sent message
        if clear == True:
            self.logger.debug('Clearing data buffer.')
            del sensor['data']


    #=============================================================================================
    # ENOCEAN TO MQTT
    #=============================================================================================

    def _publish_mqtt(self, sensor, mqtt_json):
        '''Publish decoded packet content to MQTT'''

        # Retain the to-be-published message ?
        retain = sensor.retain

        # Is grouping enabled on this sensor
        channel_id = sensor.channel
        channel_id = channel_id.split('/') if channel_id not in (None, '') else []

        # Handling Auxiliary data RSSI
        aux_data = {}
        # Publish RSSI ?
        if sensor.publish_rssi:
            # Publish using JSON format ?
            if sensor.publish_json:
                # Keep _RSSI_ out of groups
                if channel_id:
                    aux_data.update({"_RSSI_": mqtt_json['_RSSI_']})
            else:
                self.mqtt.publish(sensor.topic+"/_RSSI_", mqtt_json['_RSSI_'], retain=retain)
        # Delete RSSI if already handled
        if channel_id or not sensor.publish_json or not sensor.publish_rssi:
            del mqtt_json['_RSSI_']

        # Handling Auxiliary data _DATE_
        if str(sensor.publish_date) in ("True", "true", "1"):
            # Publish _DATE_ both at device and group levels
            if channel_id:
                if sensor.publish_json:
                    aux_data.update({"_DATE_": mqtt_json['_DATE_']})
                else:
                    self.mqtt.publish(sensor.topic+"/_DATE_", mqtt_json['_DATE_'], retain=retain)
        else:
            del mqtt_json['_DATE_']

        # Publish auxiliary data
        if aux_data:
            self.mqtt.publish(sensor.name, json.dumps(aux_data), retain=retain)

        # Determine MQTT topic
        topic = sensor.topic
        for cur_id in channel_id:
            if mqtt_json.get(cur_id) not in (None, ''):
                topic += f"/{cur_id}{mqtt_json[cur_id]}"
                del mqtt_json[cur_id]

        # Publish packet data to MQTT
        value = json.dumps(mqtt_json)
        self.logger.debug("%s: Sent MQTT: %s", topic, value)

        if sensor.publish_json:
            self.mqtt.publish(topic, value, retain=retain)
        else:
            for prop_name, value in mqtt_json.items():
                self.mqtt.publish(f"{topic}/{prop_name}", value, retain=retain)

    def _read_packet(self, packet):
        '''interpret packet, read properties and publish to MQTT'''
        mqtt_json = {}
        # loop through all configured devices
        sender_id = enocean.utils.combine_hex(packet.sender)
        self.logger.debug(f"Received packet from {sender_id}")
        equipment = self.equipments.get(sender_id)
        if equipment:
            self.logger.debug(f"Found equipment: {equipment}")
            if not packet.learn or equipment.log_learn:
                # Store RSSI
                # Use underscore so that it is unique and doesn't
                # match a potential future EnOcean EEP field.
                mqtt_json['_RSSI_'] = packet.dBm

                # Store receive date
                # Use underscore so that it is unique and doesn't
                # match a potential future EnOcean EEP field.
                mqtt_json['_DATE_'] = packet.received.isoformat()
                # Handling received data packet
                self.logger.debug(f"Handle data packet {packet}, {equipment.address}")
                found_property = self._handle_data_packet(packet, equipment, mqtt_json)
                if not found_property:
                    self.logger.warning(f"message not interpretable: {equipment.name}")
                else:
                    self._publish_mqtt(equipment, mqtt_json)
            else:
                # learn request received
                self.logger.info("learn request not emitted to mqtt")


    def _handle_data_packet(self, packet, sensor, mqtt_json):
        # data packet received
        found_property = False
        if packet.packet_type == PACKET.RADIO and packet.rorg == sensor.rorg:
            # radio packet of proper rorg type received; parse EEP
            direction = sensor.direction
            self.logger.debug(f"Handle radio packet for sensor {sensor} {packet.rorg}-{packet.rorg_func}-{packet.rorg_type}")
            # Retrieve command from the received packet and pass it to parse_eep()
            command = None
            self.logger.info(f"Try to get command for packet: {packet}")
            # if sensor.get('command'):
            command = sensor.get_command_id(packet)
            if command:
                logging.debug('Retrieved command id from packet: %s', hex(command))

            # Retrieve properties from EEP
            self.logger.debug(f"Parse EEP {sensor.func}-{sensor.type} {direction} {command}")
            # properties = packet.parse_eep(sensor.func, sensor.type, direction, command)
            message = sensor.get_message_form(command=command, direction=direction)
            properties = packet.parse_message(message)
            self.logger.debug(f"Properties {properties}")
            # loop through all EEP properties
            for prop_name in properties:
                found_property = True
                cur_prop = packet.parsed[prop_name]
                # we only extract numeric values, either the scaled ones
                # or the raw values for enums
                if isinstance(cur_prop['value'], numbers.Number):
                    value = cur_prop['value']
                else:
                    # value = cur_prop['value']
                    # mqtt_json[f"{prop_name}_raw"] = cur_prop['raw_value']
                    value = cur_prop['raw_value']
                    mqtt_json[f"{prop_name}_desc"] = cur_prop['value']
                # publish extracted information
                self.logger.debug("%s: %s (%s)=%s %s", sensor.name, prop_name,
                              cur_prop['description'], cur_prop['value'], cur_prop['unit'])

                # Store property
                mqtt_json[prop_name] = value

        return found_property


    #=============================================================================================
    # LOW LEVEL FUNCTIONS
    #=============================================================================================
    def _reply_packet(self, in_packet, sensor):
        '''send enocean message as a reply to an incoming message'''
        # prepare addresses
        destination = in_packet.sender

        self._send_packet(sensor, destination, None, True,
                          in_packet.data if in_packet.learn else None)

    def _send_packet(self, sensor, destination, command=None,
                     negate_direction=False, learn_data=None):
        '''triggers sending of an enocean packet'''
        # determine direction indicator
        direction = sensor.direction
        if negate_direction:
            # we invert the direction in this reply
            direction = 1 if direction == 2 else 2
        else:
            direction = None
        # is this a response to a learn packet?
        is_learn = learn_data is not None

        # Add possibility for the user to indicate a specific sender address
        # in sensor configuration using added 'sender' field.
        # So use specified sender address if any
        if sensor.sender:
            sender = [(sensor.sender >> i*8) & 0xff for i in reversed(range(4))]
        else:
            sender = self.enocean_sender

        try:
            # Now pass command to RadioPacket.create()
            self.logger.debug(f"Create packet {sensor.rorg}-{sensor.func}-{sensor.type} direction={direction} command={command} sender={sender} destination={destination} learn={is_learn}")
            packet = RadioPacket.create(sensor.rorg, sensor.func, sensor.type,
                                    direction=direction, command=command, sender=sender,
                                    destination=destination, learn=is_learn)
        except ValueError as err:
            logging.error("Cannot create RF packet: %s", err)
            return

        # assemble data based on packet type (learn / data)
        if not is_learn:
            # data packet received
            # start with default data

            # Initialize packet with default_data if specified
            if sensor.default_data:
                packet.data[1:5] = [(sensor.default_data >> i*8) &
                                    0xff for i in reversed(range(4))]

            # do we have specific data to send?
            if sensor.data:
                # override with specific data settings
                logging.debug("sensor data: %s", sensor.data)
                # Set packet data payload
                packet.set_eep(sensor.data)
                # Set packet status bits
                packet.data[-1] = packet.status
                packet.parse_eep()  # ensure that the logging output of packet is updated
            else:
                # what to do if we have no data to send yet?
                logging.warning('sending only default data as answer to %s', sensor.name)

        else:
            # learn request received
            # copy EEP and manufacturer ID
            packet.data[1:5] = learn_data[1:5]
            # update flags to acknowledge learn request
            packet.data[4] = 0xf0

        # send it
        logging.info("sending: %s", packet)
        self.enocean.send(packet)

    def _process_radio_packet(self, packet):
        # first, look whether we have this sensor configured
        sender_address = enocean.utils.combine_hex(packet.sender)
        found_sensor = self.equipments.get(sender_address)

        # skip ignored sensors
        if found_sensor and found_sensor.ignore:
            return

        # log packet, if not disabled
        if str(self.conf.get('log_packets')) in ("True", "true", "1"):
            self.logger.info("received: %s", packet)

        # abort loop if sensor not found
        if not found_sensor:
            self.logger.info("unknown sensor: %s", enocean.utils.to_hex_string(packet.sender))
            return

        # Handling EnOcean library decision to set learn to True by default.
        # Only 1BS and 4BS are correctly handled by the EnOcean library.
        # -> VLD EnOcean devices use UTE as learn mechanism
        if found_sensor.rorg == RORG.VLD and packet.rorg != RORG.UTE:
            packet.learn = False
        # -> RPS EnOcean devices only send normal data telegrams.
        # Hence learn can always be set to false
        elif found_sensor.rorg == RORG.RPS:
            packet.learn = False

        # interpret packet, read properties and publish to MQTT
        self._read_packet(packet)

        # check for neccessary reply
        if found_sensor.answer:
            self._reply_packet(packet, found_sensor)


    #=============================================================================================
    # RUN LOOP
    #=============================================================================================
    def run(self):
        """the main loop with blocking enocean packet receive handler"""
        # start endless loop for listening
        while self.enocean.is_alive():
            # Request transmitter ID, if needed
            if self.enocean_sender is None:
                self.enocean_sender = self.enocean.base_id
                self.logger.info(f"Set base id {self.enocean_sender}")

            # Loop to empty the queue...
            try:
                # get next packet
                if platform.system() == 'Windows':
                    # only timeout on Windows for KeyboardInterrupt checking
                    packet = self.enocean.receive.get(block=True, timeout=1)
                else:
                    packet = self.enocean.receive.get(block=True)

                # check packet type
                if packet.packet_type == PACKET.RADIO:
                    self._process_radio_packet(packet)
                elif packet.packet_type == PACKET.RESPONSE:
                    response_code = RETURN_CODE(packet.data[0])
                    logging.info("got response packet: %s", response_code.name)
                else:
                    logging.info("got non-RF packet: %s", packet)
                    continue
            except queue.Empty:
                continue
            except KeyboardInterrupt:
                logging.debug("Exception: KeyboardInterrupt")
                break

        # Run finished, close MQTT client and stop Enocean thread
        logging.debug("Cleaning up")
        self.mqtt.loop_stop()
        self.mqtt.disconnect()
        self.mqtt.loop_forever()  # will block until disconnect complete
        self.enocean.stop()
