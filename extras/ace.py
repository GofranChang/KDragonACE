import serial, time, logging, json, struct, queue, traceback # type: ignore
from datetime import datetime

class PeekableQueue(queue.Queue):
    def peek(self):
        with self.mutex:  # 使用内部锁保证线程安全
            if len(self.queue) == 0:
                return None
            return self.queue[0]

class KDragonACE:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self._name = config.get_name()
        if self._name.startswith('ace '):
            self._name = self._name[4:]
        self.variables = self.printer.lookup_object('save_variables').allVariables

        self.serial_name = config.get('serial', '/dev/ttyACM0')
        self.baud = config.getint('baud', 115200)
        self.extruder_sensor_pin = config.get('extruder_sensor_pin', None)
        self.toolhead_sensor_pin = config.get('toolhead_sensor_pin', None)
        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.toolchange_retract_length = config.getint('toolchange_retract_length', 100)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.disable_assist_after_toolchange = config.getboolean('disable_assist_after_toolchange', False)

        self._callback_map = {}
        self.park_hit_count = 5
        self._feed_assist_index = -1
        self._last_assist_count = 0
        self._assist_hit_count = 0
        self._park_in_progress = False
        self._park_is_toolchange = False
        self._park_previous_tool = -1
        self._park_index = -1

        self._last_get_ace_response_time = None

        # Default data to prevent exceptions
        self._info = {
            'status': 'ready',
            'dryer_status': {
                'status': 'stop',
                'target_temp': 0,
                'duration': 0,
                'remain_time': 0
            },
            'temp': 0,
            'enable_rfid': 1,
            'fan_speed': 7000,
            'feed_assist_count': 0,
            'cont_assist_time': 0.0,
            'current_feed_assist_slot': -1,
            'slots': [
                {
                    'index': 0,
                    'status': 'empty',
                    'sku': '',
                    'type': '',
                    'color': [0, 0, 0]
                },
                {
                    'index': 1,
                    'status': 'empty',
                    'sku': '',
                    'type': '',
                    'color': [0, 0, 0]
                },
                {
                    'index': 2,
                    'status': 'empty',
                    'sku': '',
                    'type': '',
                    'color': [0, 0, 0]
                },
                {
                    'index': 3,
                    'status': 'empty',
                    'sku': '',
                    'type': '',
                    'color': [0, 0, 0]
                }
            ]
        }

        # 载入持久化的料槽配置信息（类型/颜色/SKU）
        try:
            self._saved_slot_config = self._load_saved_slot_config()
        except Exception:
            self._saved_slot_config = {}
        self._apply_saved_slot_config_to_info()

        # 创建MMU传感器（多材料单元传感器）- 仅在配置了引脚时创建
        if self.extruder_sensor_pin is not None:
            self._create_mmu_sensor(config, self.extruder_sensor_pin, 'extruder_sensor')
        if self.toolhead_sensor_pin is not None:
            self._create_mmu_sensor(config, self.toolhead_sensor_pin, 'toolhead_sensor')

        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)

        gcode_macro = self.printer.load_object(config, "gcode_macro")
        self.pause_macro = gcode_macro.load_template(
            config, "pause_gcode", "PAUSE"
        )

        self.gcode.register_command(
            'ACE_GET_CUR_INDEX', self.cmd_ACE_GET_CUR_INDEX,
            desc=self.cmd_ACE_GET_CUR_INDEX_help
        )
        self.gcode.register_command(
            'ACE_START_DRYING', self.cmd_ACE_START_DRYING,
            desc=self.cmd_ACE_START_DRYING_help)
        self.gcode.register_command(
            'ACE_STOP_DRYING', self.cmd_ACE_STOP_DRYING,
            desc=self.cmd_ACE_STOP_DRYING_help)
        self.gcode.register_command(
            'ACE_ENABLE_FEED_ASSIST', self.cmd_ACE_ENABLE_FEED_ASSIST,
            desc=self.cmd_ACE_ENABLE_FEED_ASSIST_help)
        self.gcode.register_command(
            'ACE_DISABLE_FEED_ASSIST', self.cmd_ACE_DISABLE_FEED_ASSIST,
            desc=self.cmd_ACE_DISABLE_FEED_ASSIST_help)
        self.gcode.register_command(
            'ACE_FEED', self.cmd_ACE_FEED,
            desc=self.cmd_ACE_FEED_help)
        self.gcode.register_command(
            'ACE_RETRACT', self.cmd_ACE_RETRACT,
            desc=self.cmd_ACE_RETRACT_help)
        self.gcode.register_command(
            'ACE_REJECT_TOOL', self.cmd_ACE_REJECT_TOOL,
            desc=self.cmd_ACE_REJECT_TOOL_help)
        self.gcode.register_command(
            'ACE_CHANGE_TOOL', self.cmd_ACE_CHANGE_TOOL,
            desc=self.cmd_ACE_CHANGE_TOOL_help)
        self.gcode.register_command(
            'ACE_FILAMENT_STATUS', self.cmd_ACE_FILAMENT_STATUS,
            desc=self.cmd_ACE_FILAMENT_STATUS_help)
        self.gcode.register_command(
            'ACE_CLEAR_ALL_STATUS', self.cmd_ACE_CLEAR_ALL_STATUS,
            desc=self.cmd_ACE_CLEAR_ALL_STATUS_help)
        self.gcode.register_command(
            'ACE_DEBUG', self.cmd_ACE_DEBUG,
            desc=self.cmd_ACE_DEBUG_help)
        self.gcode.register_command(
            'ACE_STATUS', self.cmd_ACE_STATUS,
            desc=self.cmd_ACE_STATUS_help)
        self.gcode.register_command(
            'ACE_SET_STATUS', self.cmd_ACE_SET_STATUS,
            desc=self.cmd_ACE_SET_STATUS_help)
        self.gcode.register_command(
            'ACE_SET_SLOT_INFO', self.cmd_ACE_SET_SLOT_INFO,
            desc=self.cmd_ACE_SET_SLOT_INFO_help)


        # 注册到打印机对象，使其可以被前端查询
        self.printer.add_object("ace", self)

    def get_status(self, eventtime=None):
        """
        返回ACE设备状态，供前端查询
        这个方法会被Klipper的状态查询系统调用
        """
        return self._info.copy()

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        logging.info('ACE: Connecting to ' + self.serial_name)

        self._request_id = 0
        self._connected = False
        self._serial = None
        while not self._connected:
            self._reconnect_serial()
            self.reactor.pause(0.5)

        if not self._connected:
            raise ValueError('ACE: Failed to connect to ' + self.serial_name)

        logging.info('ACE: Connected to ' + self.serial_name)

        self._queue = PeekableQueue()
        self.serial_timer = self.reactor.register_timer(self._serial_read_write, self.reactor.NOW)

        self._main_queue = queue.Queue()
        self.main_timer = self.reactor.register_timer(self._main_eval, self.reactor.NOW)

        def info_callback(self, response):
            res = response['result']
            self.gcode.respond_info('Connected ' + res['model'] + ' ' + res['firmware'])
        self.send_request(request = {'method': 'get_info'}, callback = info_callback)


    def _handle_disconnect(self):
        logging.info('ACE: Closing connection to ' + self.serial_name)
        self._serial.close()
        self._connected = False

        self._main_queue = None
        self.reactor.unregister_timer(self.main_timer)

        self._queue = None
        self.reactor.unregister_timer(self.serial_timer)

    def _calc_crc(self, buffer):
        _crc = 0xffff
        for byte in buffer:
            data = byte
            data ^= _crc & 0xff
            data ^= (data & 0x0f) << 4
            _crc = ((data << 8) | (_crc >> 8)) ^ (data >> 4) ^ (data << 3)
        return _crc

    def _update_and_get_request_id(self):
        if self._request_id >= 16382:
            self._request_id = 0
        else:
            self._request_id += 1

        return self._request_id


    def _write_serial(self, request):
        if not 'id' in request:
            request['id'] = self._update_and_get_request_id()

        payload = json.dumps(request)
        payload = bytes(payload, 'utf-8')

        data = bytes([0xFF, 0xAA])
        data += struct.pack('@H', len(payload))
        data += payload
        data += struct.pack('@H', self._calc_crc(payload))
        data += bytes([0xFE])

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # logging.info(f'[ACE] {now} >>> {request}')

        try:
            self._serial.write(data)
        except Exception as e:
            self.gcode.respond_info(f'[ACE] serial write exception {e}')
            return False

        return True


    def _main_eval(self, eventtime):
        while not self._main_queue.empty():
            task = self._main_queue.get_nowait()
            if task is not None:
                task()

        return eventtime + 0.25

    def _reconnect_serial(self):
        if self._connected:
            self.gcode.respond_warn('[ACE] reconnect warning: serial port already connected')
            return True

        try:
            if self._serial != None and self._serial.isOpen():
                self._serial.close()
                self._connected = False

            self._serial = serial.Serial(port=self.serial_name,
                                        baudrate=self.baud)
            if self._serial.isOpen():
                self._connected = True

                if self._feed_assist_index != -1:
                    self._enable_feed_assist(self._feed_assist_index)
                self.gcode.respond_info('[ACE] Reconnected successfully.')
                return True
        except Exception as e:
            logging.warning(f'[ACE] reconnect error: {e}')

        return False

    def _send_heartbeat(self, id):
        def callback(self, response):
            if response is not None:
                # self._info = response['result']
                # 只更新ACE设备实际提供的状态字段，避免覆盖本地状态
                result = response['result']
                if 'status' in result:
                    self._info['status'] = result['status']
                if 'temp' in result:
                    self._info['temp'] = result['temp']
                if 'enable_rfid' in result:
                    self._info['enable_rfid'] = result['enable_rfid']
                if 'fan_speed' in result:
                    self._info['fan_speed'] = result['fan_speed']
                if 'feed_assist_count' in result:
                    self._info['feed_assist_count'] = result['feed_assist_count']
                if 'cont_assist_time' in result:
                    self._info['cont_assist_time'] = result['cont_assist_time']
                
                # 更新料槽状态（ACE设备会提供准确的料槽状态）
                if 'slots' in result:
                    self._info['slots'] = result['slots']
                    # 将本地保存的类型/颜色/SKU 覆盖到最新的槽位状态上
                    self._apply_saved_slot_config_to_info()
                
                # 更新烘干器状态 - 与slots状态更新保持一致的频率
                if 'dryer_status' in result:
                    # 直接更新烘干器状态，与slots更新逻辑保持一致
                    if result['dryer_status']:
                        self._info['dryer_status'] = result['dryer_status']
                    else:
                        # 如果dryer_status为空，重置为默认停止状态
                        self._info['dryer_status'] = {
                            'status': 'stop',
                            'target_temp': 0,
                            'duration': 0,
                            'remain_time': 0
                        }         
                # 添加当前辅助进料料槽信息到状态中
                self._info['current_feed_assist_slot'] = self._feed_assist_index


                if self._park_in_progress and self._info['status'] == 'ready':
                    new_assist_count = self._info['feed_assist_count']
                    if new_assist_count > self._last_assist_count:
                        self._last_assist_count = new_assist_count
                        self.dwell(0.7, True) # 0.68 + small room 0.02 for response
                        self._assist_hit_count = 0
                    elif self._assist_hit_count < self.park_hit_count:
                        self._assist_hit_count += 1
                        self.dwell(0.7, True)
                    else:
                        self._assist_hit_count = 0
                        self._park_in_progress = False
                        logging.info('ACE: Parked to toolhead with assist count: ' + str(self._last_assist_count))

                        if self._park_is_toolchange:
                            self._park_is_toolchange = False
                            def main_callback():
                                self.gcode.run_script_from_command('_ACE_POST_TOOLCHANGE FROM=' + str(self._park_previous_tool) + ' TO=' + str(self._park_index))
                            self._main_queue.put(main_callback)
                        else:
                            self.send_request(request = {'method': 'stop_feed_assist', 'params': {'index': self._park_index}}, callback=None)

        self._callback_map[id] = callback
        if not self._write_serial({'id': id, 'method': 'get_status'}):
            return False

        return True

    def _reader(self):
        data = None

        for i in range(0, 2):
            try:
                data = self._serial.read_until(expected=bytes([0xFE]), size=4096)
            except Exception as e:
                self.gcode.respond_info(f'[ACE] read exception {e}')
                return None

            if None != data and len(data) >= 7:
                break

        if None == data or len(data) < 7:
            logging.info(f'[ACE] Read Too short')
            return None

        if data[0:2] != b"\xFF\xAA":
            logging.info(f'[ACE] Read invalid header')
            return None

        payload_length = struct.unpack("@H", data[2:4])[0]
        payload = data[4 : 4 + payload_length]
        # crc_received = data[4 + payload_length : 4 + payload_length + 2]

        # calculated_crc = self._calc_crc(payload)
        # if calculated_crc != crc_received:
        #     logging.info(f'[ACE] Read invalid CRC')
        #     return None

        try:
            json_str = payload.decode("utf-8")
            ret = json.loads(json_str)
        except Exception as e:
            logging.info(f'[ACE] Read invalid JSON')
            return None

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # logging.info(f'[ACE] {now} <<< {ret}')
        id = ret['id']
        if id in self._callback_map:
            callback = self._callback_map.pop(id)
            if callback != None:
                callback(self = self, response = ret)

        return id

    def _writer(self):
        id = self._update_and_get_request_id()

        if not self._queue.empty():
            task = self._queue.peek()
            if task is not None:
                task[0]['id'] = id
                self._callback_map[id] = task[1]

                if not self._write_serial(task[0]):
                    if not task[2]:
                        # Not Retry
                        self._queue.get()

                    return None

                self._queue.get()

        else:
            if not self._send_heartbeat(id):
                return None

        return id

    def _serial_read_write(self, eventtime):
        if self._connected:
            send_id = self._writer()
            if None == send_id:
                self._connected = False
                return eventtime + 1

            read_id = self._reader()
            if read_id != send_id:
                self._connected = False
                return eventtime + 1
        else:
            self._reconnect_serial()
            return eventtime + 1

        if self._park_in_progress:
            next_time = 0.68
        else:
            next_time = 0.25
        return eventtime + next_time

    def wait_ace_ready(self):
        while self._info['status'] != 'ready':
            self.dwell(delay=0.5)


    def send_request(self, request, callback, with_retry=True):
        self._queue.put([request, callback, with_retry])


    def dwell(self, delay = 1., on_main = False):
        def main_callback():
            self.toolhead.dwell(delay)

        if on_main:
            self._main_queue.put(main_callback)
        else:
            main_callback()

    def _extruder_move(self, length, speed):
        pos = self.toolhead.get_position()
        pos[3] += length
        self.toolhead.move(pos, speed)
        return pos[3]

    def _create_mmu_sensor(self, config, pin, name):
        section = 'filament_switch_sensor %s' % name
        config.fileconfig.add_section(section)
        config.fileconfig.set(section, 'switch_pin', pin)
        config.fileconfig.set(section, 'pause_on_runout', 'False')
        fs = self.printer.load_object(config, section)

    def _feed(self, index, length, speed):
        def callback(self, response):
            if 'code' in response and response['code'] != 0:
                raise ValueError('ACE Error: ' + response['msg'])
            # 如果进行手动进料，辅助进料会被中断
            self._feed_assist_index = -1

        self.send_request(request = {'method': 'feed_filament', 'params': {'index': index, 'length': length, 'speed': speed}}, callback = callback)
        self.dwell(delay = (length / speed) + 0.1)

    def _retract(self, index, length, speed):
        def callback(self, response):
            if 'code' in response and response['code'] != 0:
                raise ValueError('ACE Error: ' + response['msg'])
            # 如果进行手动退料，辅助进料会被中断
            self._feed_assist_index = -1

        self.send_request(
            request={'method': 'unwind_filament', 'params': {'index': index, 'length': length, 'speed': speed}},
            callback=callback)
        self.dwell(delay=(length / speed) + 0.1)

    def _enable_feed_assist(self, index):
        def callback(self, response):
            if 'code' in response and response['code'] != 0:
                raise ValueError('ACE Error: ' + response['msg'])
            else:
                self._feed_assist_index = index
                # self.gcode.respond_info(str(response))

        self.send_request(request = {'method': 'start_feed_assist', 'params': {'index': index}}, callback = callback)
        self.dwell(delay = 0.7)

    def _disable_feed_assist(self, index):
        def callback(self, response):
            if 'code' in response and response['code'] != 0:
                raise self.gcode.error('ACE Error: ' + response['msg'])

            self._feed_assist_index = -1
            self.gcode.respond_info('Disabled ACE feed assist')

        self.send_request(request = {'method': 'stop_feed_assist', 'params': {'index': index}}, callback = callback)
        self.dwell(0.3)

    def _save_to_disk(self):
        self.gcode.run_script_from_command('SAVE_VARIABLE VARIABLE=ace_current_index VALUE=' + str(self.variables['ace_current_index']))
        self.gcode.run_script_from_command(f"""SAVE_VARIABLE VARIABLE=ace_filament_pos VALUE='"{self.variables['ace_filament_pos']}"'""")

    def _park_to_toolhead(self, tool):
        sensor_extruder = self.printer.lookup_object('filament_switch_sensor %s' % 'extruder_sensor', None)
        sensor_toolhead = self.printer.lookup_object('filament_switch_sensor %s' % 'toolhead_sensor', None)

        self._enable_feed_assist(tool)

        for _ in range(20):
            if bool(sensor_extruder.runout_helper.filament_present):
                break
            self._feed(tool, 40, self.feed_speed)
            self.dwell(delay=2)
        else:
            self.variables['ace_filament_pos'] = 'spliter'
            self.gcode.respond_info('Filament not in the Extruder')
            self._send_pause()
            return
            # raise ValueError('Filament stuck at bowden' + str(bool(sensor_toolhead.runout_helper.filament_present)))
        self._feed(tool, 20, self.feed_speed)
        self._enable_feed_assist(tool)
        for _ in range(10):
            if bool(sensor_toolhead.runout_helper.filament_present):
                break
            self._extruder_move(20, 5)
            self.dwell(delay=1)
        else:
            #raise ValueError('Filament stuck at toolhead ' + str(bool(sensor_extruder.runout_helper.filament_present)))
            self.gcode.respond_info('Filament not in the Toolhead')
            self._send_pause()
            return
        self._enable_feed_assist(tool)
        self.variables['ace_filament_pos'] = 'toolhead'

        # The nozzle should be cleaned by brushing
        self.variables['ace_filament_pos'] = 'nozzle'

        if self.disable_assist_after_toolchange:
            self.send_request({"method": "stop_feed_assist", "params": {"index": tool}}, callback=None)

    def _reject_tool(self, index):
        self.gcode.respond_info(f'ACE: reject tool {index}')
        sensor_extruder = self.printer.lookup_object('filament_switch_sensor %s' % 'extruder_sensor', None)

        self._disable_feed_assist(index)
        self.wait_ace_ready()
        if  self.variables.get('ace_filament_pos', 'spliter') == 'nozzle':
            self.gcode.respond_info(f'ACE: cut tool {index}')
            self.gcode.run_script_from_command('CUT_TIP')
            self.variables['ace_filament_pos'] = 'toolhead'

        if  self.variables.get('ace_filament_pos', 'spliter') == 'toolhead':
            self.gcode.respond_info(f'ACE: extract tool {index} out of the extruder')
            while bool(sensor_extruder.runout_helper.filament_present):
                self._extruder_move(-20, 5)
                self._retract(index, 20, self.retract_speed)
                self.dwell(1)
            self.variables['ace_filament_pos'] = 'bowden'

        self.wait_ace_ready()

        self.gcode.respond_info(f'ACE: extract tool {index} out of the hub')
        self._retract(index, self.toolchange_retract_length, self.retract_speed)
        self.variables['ace_filament_pos'] = 'spliter'

        self.wait_ace_ready()

        self.gcode.respond_info(f'ACE: set current index -1')
        self.variables['ace_current_index'] = -1

        self._save_to_disk()

    def _send_pause(self):
        pause_resume = self.printer.lookup_object("pause_resume")
        if pause_resume.get_status(self.reactor.monotonic())["is_paused"]:
            return

        # run pause macro
        self.pause_macro.run_gcode_from_command()
    
    def _load_saved_slot_config(self):
        """从 save_variables 载入已保存的料槽信息。
        优先兼容旧的单字典键 'ace_slots_config'，并合并每个独立键：
        ace_slot_{i}_type, ace_slot_{i}_color, ace_slot_{i}_sku
        返回格式：{ index: { 'type': str, 'color': [r,g,b], 'sku': str } }
        """
        merged = {}
        try:
            for i in range(4):
                t = self.variables.get(f'ace_slot_{i}_type', None)
                c = self.variables.get(f'ace_slot_{i}_color', None)
                s = self.variables.get(f'ace_slot_{i}_sku', None)
                if t is None and c is None and s is None:
                    continue
                if i not in merged:
                    merged[i] = {}
                if isinstance(t, str) and t:
                    merged[i]['type'] = t
                if isinstance(c, (list, tuple)) and len(c) >= 3:
                    try:
                        r, g, b = int(c[0]), int(c[1]), int(c[2])
                        merged[i]['color'] = [r, g, b]
                    except Exception:
                        pass
                if isinstance(s, str):
                    merged[i]['sku'] = s
        except Exception:
            pass

        return merged

    def _persist_slot_index(self, idx):
        """将指定槽位的键分别持久化为独立变量，避免字典整体保存导致的命令格式问题。"""
        if not hasattr(self, '_saved_slot_config'):
            return
        data = self._saved_slot_config.get(idx, {})
        # TYPE
        if 'type' in data:
            t = data.get('type', '')
            if isinstance(t, str):
                self.variables[f'ace_slot_{idx}_type'] = t
                # 传递 python 字面量字符串，使用引号
                self.gcode.run_script_from_command(
                    f'SAVE_VARIABLE VARIABLE=ace_slot_{idx}_type VALUE="{repr(t)}"')
        # SKU
        if 'sku' in data:
            s = data.get('sku', '')
            if isinstance(s, str):
                self.variables[f'ace_slot_{idx}_sku'] = s
                self.gcode.run_script_from_command(
                    f'SAVE_VARIABLE VARIABLE=ace_slot_{idx}_sku VALUE="{repr(s)}"')
        # COLOR
        if 'color' in data:
            c = data.get('color')
            if isinstance(c, (list, tuple)) and len(c) >= 3:
                try:
                    r, g, b = int(c[0]), int(c[1]), int(c[2])
                    c_list = [max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))]
                except Exception:
                    c_list = None
                if c_list is not None:
                    self.variables[f'ace_slot_{idx}_color'] = c_list
                    # 列表作为 python 字面量，整体用双引号包裹为一个 token
                    self.gcode.run_script_from_command(
                        f'SAVE_VARIABLE VARIABLE=ace_slot_{idx}_color VALUE="{c_list}"')

    def _apply_saved_slot_config_to_info(self):
        """将本地保存的槽位设置覆盖到 self._info['slots'] 上。"""
        try:
            if not hasattr(self, '_saved_slot_config') or not self._saved_slot_config:
                return
            slots = self._info.get('slots', [])
            for slot in slots:
                idx = slot.get('index', -1)
                if idx in self._saved_slot_config:
                    saved = self._saved_slot_config[idx]
                    # 覆盖类型
                    t = saved.get('type', '')
                    if isinstance(t, str) and t:
                        slot['type'] = t
                    # 覆盖颜色
                    c = saved.get('color', None)
                    if isinstance(c, list) and len(c) >= 3:
                        try:
                            r, g, b = int(c[0]), int(c[1]), int(c[2])
                            slot['color'] = [max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))]
                        except Exception:
                            pass
                    # 覆盖 SKU
                    sku = saved.get('sku', None)
                    if isinstance(sku, str):
                        slot['sku'] = sku
        except Exception as e:
            logging.warning(f"[ACE] Failed to apply saved slot config: {e}")

    cmd_ACE_GET_CUR_INDEX_help = 'Get current tool index'
    def cmd_ACE_GET_CUR_INDEX(self, gcmd):
        self.gcode.respond_info('ACE Current index {}'.format(self.variables['ace_current_index']))

    cmd_ACE_START_DRYING_help = 'Starts ACE Pro dryer'
    def cmd_ACE_START_DRYING(self, gcmd):
        temperature = gcmd.get_int('TEMP')
        duration = gcmd.get_int('DURATION', 240)

        if duration <= 0:
            raise gcmd.error('Wrong duration')
        if temperature <= 0 or temperature > self.max_dryer_temperature:
            raise gcmd.error('Wrong temperature')

        def callback(self, response):
            if 'code' in response and response['code'] != 0:
                raise gcmd.error('ACE Error: ' + response['msg'])
            # 更新字典
            self._info['dryer_status']['status'] = 'drying'
            self._info['dryer_status']['target_temp'] = temperature
            self._info['dryer_status']['duration'] = duration
            self._info['dryer_status']['remain_time'] = duration

            self.gcode.respond_info('Started ACE drying')

        self.send_request(request = {'method': 'drying', 'params': {'temp':temperature, 'fan_speed': 7000, 'duration': duration}}, callback = callback)


    cmd_ACE_STOP_DRYING_help = 'Stops ACE Pro dryer'
    def cmd_ACE_STOP_DRYING(self, gcmd):
        def callback(self, response):
            if 'code' in response and response['code'] != 0:
                raise gcmd.error('ACE Error: ' + response['msg'])
            
            self._info['dryer_status']['status'] = 'stop'
            self._info['dryer_status']['target_temp'] = 0
            self._info['dryer_status']['duration'] = 0
            self._info['dryer_status']['remain_time'] = 0

            self.gcode.respond_info('Stopped ACE drying')

        self.send_request(request = {'method':'drying_stop'}, callback = callback)

    cmd_ACE_ENABLE_FEED_ASSIST_help = 'Enables ACE feed assist'
    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX')

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')

        self._enable_feed_assist(index)

    cmd_ACE_DISABLE_FEED_ASSIST_help = 'Disables ACE feed assist'
    def cmd_ACE_DISABLE_FEED_ASSIST(self, gcmd):
        if self._feed_assist_index != -1:
            index = gcmd.get_int('INDEX', self._feed_assist_index)
        else:
            index = gcmd.get_int('INDEX')

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')

        self._disable_feed_assist(index)

    cmd_ACE_FEED_help = 'Feeds filament from ACE'
    def cmd_ACE_FEED(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.feed_speed)

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')

        self._feed(index, length, speed)

    cmd_ACE_RETRACT_help = 'Retracts filament back to ACE'
    def cmd_ACE_RETRACT(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.retract_speed)

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')

        self._retract(index, length, speed)

    cmd_ACE_CLEAR_ALL_STATUS_help = 'Clean status'
    def cmd_ACE_CLEAR_ALL_STATUS(self, gcmd):
        self.variables['ace_current_index'] = -1
        self.variables['ace_filament_pos'] = 'spliter'
        self._save_to_disk()

    cmd_ACE_REJECT_TOOL_help = 'Reject tool'
    def cmd_ACE_REJECT_TOOL(self, gcmd):
        tool = gcmd.get_int('TOOL', -1)

        if -1 == tool:
            tool = self.variables.get('ace_current_index', -1)
        if tool != -1:
            self._reject_tool(tool)

    cmd_ACE_CHANGE_TOOL_help = 'Changes tool'
    def cmd_ACE_CHANGE_TOOL(self, gcmd):
        # self.gcode.respond_info('ACE: Changing tool...')
        tool = gcmd.get_int('TOOL')

        if tool < -1 or tool >= 4:
            raise gcmd.error('Wrong tool')

        was = self.variables.get('ace_current_index', -1)
        if was == tool:
            gcmd.respond_info('ACE: Not changing tool, current index already ' + str(tool))
            return

        if tool != -1:
            status = self._info['slots'][tool]['status']
            if status != 'ready':
                self.gcode.run_script_from_command('_ACE_ON_EMPTY_ERROR INDEX=' + str(tool))
                return

        self.gcode.run_script_from_command('_ACE_PRE_TOOLCHANGE FROM=' + str(was) + ' TO=' + str(tool))

        logging.info('ACE: Toolchange ' + str(was) + ' => ' + str(tool))
        if was != -1:
            self._reject_tool(was)

        if tool != -1:
            self._feed(tool, self.toolchange_retract_length-5, self.retract_speed)
            self.variables['ace_filament_pos'] = 'bowden'
            self.wait_ace_ready()

            self._park_to_toolhead(tool)

        self.gcode.run_script_from_command('_ACE_POST_TOOLCHANGE FROM=' + str(was) + ' TO=' + str(tool))

        self.variables['ace_current_index'] = tool
        # Force save to disk
        self._save_to_disk()
        # self.gcode.run_script_from_command('SAVE_VARIABLE VARIABLE=ace_current_index VALUE=' + str(tool))
        # self.gcode.run_script_from_command(f"""SAVE_VARIABLE VARIABLE=ace_filament_pos VALUE='"{self.variables['ace_filament_pos']}"'""")

        gcmd.respond_info(f'Tool {tool} load')


    cmd_ACE_FILAMENT_STATUS_help = 'ACE Filament status'
    def cmd_ACE_FILAMENT_STATUS(self, gcmd):
        sensor_extruder = self.printer.lookup_object('filament_switch_sensor %s' % 'extruder_sensor', None)
        sensor_toolhead = self.printer.lookup_object('filament_switch_sensor %s' % 'toolhead_sensor', None)
        state = 'ACE----------|*--|Ex--|*----|Nz--'
        if  self.variables['ace_filament_pos'] == 'nozzle':
            state = 'ACE>>>>>>>>>>|*>>|Ex>>|*>>|Nz>>'
        if  self.variables['ace_filament_pos'] == 'toolhead' and bool(sensor_toolhead.runout_helper.filament_present):
            state = 'ACE>>>>>>>>>>|*>>|Ex>>|*>>|Nz--'
        if  self.variables['ace_filament_pos'] == 'toolhead' and not bool(sensor_toolhead.runout_helper.filament_present):
            state = 'ACE>>>>>>>>>>|*>>|Ex>>|*--|Nz--'
        if  self.variables['ace_filament_pos'] == 'bowden' and bool(sensor_extruder.runout_helper.filament_present):
            state = 'ACE>>>>>>>>>>|*>>|Ex--|*--|Nz--'
        if  self.variables['ace_filament_pos'] == 'bowden' and not bool(sensor_extruder.runout_helper.filament_present):
            state = 'ACE>>>>>>>>>>|*--|Ex--|*--|Nz--'
        gcmd.respond_info(state)

    cmd_ACE_DEBUG_help = 'ACE Debug'
    def cmd_ACE_DEBUG(self, gcmd):
        method = gcmd.get('METHOD')
        params = gcmd.get('PARAMS', '{}')

        try:
            def callback(self, response):
                self.gcode.respond_info(str(response))

            self.send_request(request = {'method': method, 'params': json.loads(params)}, callback = callback)
        except Exception as e:
            self.gcode.respond_info('Error: ' + str(e))

    cmd_ACE_STATUS_help = 'Get ACE device status'
    def cmd_ACE_STATUS(self, gcmd):
        """
        G-code命令：ACE_STATUS
        获取并显示ACE设备的完整状态信息
        
        示例: ACE_STATUS
        """
        def callback(self, response):
            if 'code' in response and response['code'] != 0:
                self.gcode.respond_info('ACE Error: ' + response['msg'])
                return
            
            result = response.get('result', {})
            
            # 同步ACE设备的最新状态到本地（强制同步，因为这是显式查询）
            if result:
                # 更新设备基本状态
                if 'status' in result:
                    self._info['status'] = result['status']
                if 'temp' in result:
                    self._info['temp'] = result['temp']
                if 'enable_rfid' in result:
                    self._info['enable_rfid'] = result['enable_rfid']
                if 'fan_speed' in result:
                    self._info['fan_speed'] = result['fan_speed']
                if 'feed_assist_count' in result:
                    self._info['feed_assist_count'] = result['feed_assist_count']
                if 'cont_assist_time' in result:
                    self._info['cont_assist_time'] = result['cont_assist_time']
                
                # 更新料槽状态（ACE设备会提供准确的料槽状态）
                if 'slots' in result:
                    self._info['slots'] = result['slots']
                
                # 更新烘干器状态（强制同步，因为这是显式查询）
                if 'dryer_status' in result and result['dryer_status']:
                    self._info['dryer'] = result['dryer_status']
            
            # 显示设备基本状态
            self.gcode.respond_info(f"ACE Device Status: {self._info.get('status', 'unknown')}")
            self.gcode.respond_info(f"Temperature: {self._info.get('temp', 0)}°C")
            self.gcode.respond_info(f"Fan Speed: {self._info.get('fan_speed', 0)} RPM")
            self.gcode.respond_info(f"Feed Assist Count: {self._info.get('feed_assist_count', 0)}")
            
            # 显示烘干器状态
            dryer = self._info.get('dryer', {})
            self.gcode.respond_info(f"Dryer Status: {dryer.get('status', 'stop')}")
            if dryer.get('status') == 'running':
                self.gcode.respond_info(f"Dryer Target Temp: {dryer.get('target_temp', 0)}°C")
                self.gcode.respond_info(f"Dryer Duration: {dryer.get('duration', 0)} min")
                self.gcode.respond_info(f"Dryer Remain Time: {dryer.get('remain_time', 0)} min")
            
            # 显示料槽状态
            slots = self._info.get('slots', [])
            self.gcode.respond_info("Slot Status:")
            for slot in slots:
                index = slot.get('index', 0)
                status = slot.get('status', 'empty')
                sku = slot.get('sku', '')
                slot_type = slot.get('type', '')
                color = slot.get('color', [0, 0, 0])
                
                slot_info = f"  Slot {index}: {status}"
                if sku:
                    slot_info += f", SKU: {sku}"
                if slot_type:
                    slot_info += f", Type: {slot_type}"
                if color != [0, 0, 0]:
                    slot_info += f", Color: RGB({color[0]}, {color[1]}, {color[2]})"
                
                self.gcode.respond_info(slot_info)

        self.send_request(request={'method': 'get_status'}, callback=callback)

    cmd_ACE_SET_STATUS_help = 'Set ACE status manully'
    def cmd_ACE_SET_STATUS(self, gcmd):
        # 允许省略任意参数；未提供时保持当前值
        current_index = self.variables.get('ace_current_index', -1)
        current_pos = self.variables.get('ace_filament_pos', 'spliter')

        # 解析 INDEX（可选）
        index_raw = gcmd.get('INDEX', None)
        if index_raw is None or index_raw == '':
            index = current_index
        else:
            try:
                index = int(index_raw)
            except Exception:
                raise gcmd.error('INDEX must be an integer in range -1..3')
            if index < -1 or index > 3:
                raise gcmd.error('Wrong index: expected -1..3')

        # 解析 POS（可选，大小写不敏感）
        position_raw = gcmd.get('POS', None)
        if position_raw is None or position_raw == '':
            position = current_pos
        else:
            position = str(position_raw).strip().lower()
            valid_positions = {'spliter', 'bowden', 'toolhead', 'nozzle'}
            if position not in valid_positions:
                raise gcmd.error('Wrong POS: expected one of spliter|bowden|toolhead|nozzle')

        # 语义约束：当位置为 toolhead/nozzle 时，应当存在有效的工具索引
        if position in ('toolhead', 'nozzle') and index == -1:
            raise gcmd.error('POS implies filament at toolhead/nozzle, require INDEX in 0..3')

        # 无变更时直接提示并返回
        if index == current_index and position == current_pos:
            gcmd.respond_info('ACE: status unchanged')
            return

        # 应用变更并持久化
        self.variables['ace_filament_pos'] = position
        self.variables['ace_current_index'] = index
        self._save_to_disk()

        gcmd.respond_info(f"ACE: status set INDEX={index} POS={position}")

    cmd_ACE_SET_SLOT_INFO_help = 'Set ACE slot info and persist (TYPE/COLOR/SKU)'
    def cmd_ACE_SET_SLOT_INFO(self, gcmd):
        """
        设置并保存料槽信息：
        用法:
          ACE_SET_SLOT_INFO INDEX=<0-3> [TYPE=<str>] [COLOR=<r,g,b>] [SKU=<str>]
        说明:
          - TYPE/COLOR/SKU 均为可选，提供则更新并保存
          - COLOR 需为逗号分隔的三个 0-255 整数
        """
        idx = gcmd.get_int('INDEX')
        if idx < 0 or idx >= 4:
            raise gcmd.error('Wrong index')

        type_val = gcmd.get('TYPE', None)
        color_val = gcmd.get('COLOR', None)
        sku_val = gcmd.get('SKU', None)

        if not hasattr(self, '_saved_slot_config') or self._saved_slot_config is None:
            self._saved_slot_config = {}
        if idx not in self._saved_slot_config or not isinstance(self._saved_slot_config.get(idx), dict):
            self._saved_slot_config[idx] = {}

        if type_val is not None:
            self._saved_slot_config[idx]['type'] = type_val

        if color_val is not None:
            try:
                parts = [p.strip() for p in color_val.split(',')]
                if len(parts) != 3:
                    raise ValueError('COLOR should be r,g,b')
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                r = max(0, min(255, r))
                g = max(0, min(255, g))
                b = max(0, min(255, b))
                self._saved_slot_config[idx]['color'] = [r, g, b]
            except Exception:
                raise gcmd.error('Invalid COLOR value, expected r,g,b')

        if sku_val is not None:
            self._saved_slot_config[idx]['sku'] = sku_val

        # 保存到磁盘
        self._persist_slot_index(idx)

        # 立刻覆盖当前状态，便于前端立即看到变更
        self._apply_saved_slot_config_to_info()

        # 显式触发状态响应，促使前端订阅者刷新
        try:
            self.gcode.respond_info('')
        except Exception:
            pass

        gcmd.respond_info(f'ACE: slot {idx} info saved')

def load_config(config):
    return KDragonACE(config)
