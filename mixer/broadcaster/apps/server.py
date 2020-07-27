from __future__ import annotations

import logging
import argparse
import select
import threading
import socket
import queue
from typing import Tuple, List, Mapping, Dict, Optional, Any

from mixer.broadcaster.cli_utils import init_logging, add_logging_cli_args
import mixer.broadcaster.common as common
from mixer.broadcaster.common import update_dict_and_get_diff

SHUTDOWN = False

logger = logging.getLogger() if __name__ == "__main__" else logging.getLogger(__name__)


class Connection:
    """ Represent a connection with a client """

    def __init__(self, server: Server, socket, address):
        self.socket = socket
        self.address = address
        self.room: Optional[Room] = None

        self.metadata: Dict[str, Any] = {}  # metadata are used between clients, but not by the server

        self._command_queue: queue.Queue = queue.Queue()  # Pending commands to send to the client
        self._server = server
        # optimization to avoid too much messages when broadcasting client/room metadata updates
        self._list_all_clients_flag = False
        self._list_rooms_flag = False

    def start(self):
        self.thread = threading.Thread(None, self.run)
        self.thread.start()

    def join_room(self, room_name: str):
        error = None
        if self.room is not None:
            error = f"Received join_room({room_name}) but room {self.room.name} is already joined"

        if error:
            logger.warning(error)
            self.send_error(error)
            return

        self._server.join_room(self, room_name)

    def leave_room(self, room_name: str):
        error = None
        if self.room is None:
            error = f"Received leave_room({room_name}) but no room is joined"
        elif room_name != self.room.name:
            error = f"Received leave_room({room_name}) but room {self.room.name} is joined instead"

        if error:
            logger.warning(error)
            self.send_error(error)
            return

        self._server.leave_room(self, room_name)

    def send_list_rooms(self):
        self._list_rooms_flag = True

    def send_client_ids(self):
        self._list_all_clients_flag = True

    def get_unique_id(self) -> str:
        return f"{self.address[0]}:{self.address[1]}"

    def client_id(self) -> Dict[str, Any]:
        return {
            **self.metadata,
            common.ClientMetadata.ID: f"{self.get_unique_id()}",
            common.ClientMetadata.IP: self.address[0],
            common.ClientMetadata.PORT: self.address[1],
            common.ClientMetadata.ROOM: self.room.name if self.room is not None else None,
        }

    def set_client_metadata(self, metadata: Mapping[str, Any]):
        diff = update_dict_and_get_diff(self.metadata, metadata)
        self._server.broadcast_client_update(self, diff)

    def send_error(self, s: str):
        logger.debug("Sending error %s", s)
        command = common.Command(common.MessageType.SEND_ERROR, common.encode_string(s))
        self.add_command(command)

    def run(self):
        global SHUTDOWN
        while not SHUTDOWN:
            try:
                command = common.read_message(self.socket)
            except common.ClientDisconnectedException:
                break

            if command is not None:
                logger.debug("Received from %s:%s - %s", self.address[0], self.address[1], command.type)

                if command.type == common.MessageType.JOIN_ROOM:
                    self.join_room(command.data.decode())

                elif command.type == common.MessageType.LEAVE_ROOM:
                    self.leave_room(command.data.decode())

                elif command.type == common.MessageType.LIST_ROOMS:
                    self.send_list_rooms()

                elif command.type == common.MessageType.DELETE_ROOM:
                    self._server.delete_room(command.data.decode())

                elif command.type == common.MessageType.SET_CLIENT_NAME:
                    self.set_client_metadata({common.ClientMetadata.USERNAME: command.data.decode()})

                elif command.type == common.MessageType.LIST_ALL_CLIENTS:
                    self.send_client_ids()

                elif command.type == common.MessageType.SET_CLIENT_METADATA:
                    self.set_client_metadata(common.decode_json(command.data, 0)[0])

                elif command.type == common.MessageType.SET_ROOM_METADATA:
                    room_name, offset = common.decode_string(command.data, 0)
                    metadata, _ = common.decode_json(command.data, offset)
                    self._server.set_room_metadata(room_name, metadata)

                elif command.type == common.MessageType.SET_ROOM_KEEP_OPEN:
                    room_name, offset = common.decode_string(command.data, 0)
                    value, _ = common.decode_bool(command.data, offset)
                    self._server.set_room_keep_open(room_name, value)

                elif command.type == common.MessageType.CLIENT_ID:
                    self.add_command(
                        common.Command(
                            common.MessageType.CLIENT_ID, f"{self.address[0]}:{self.address[1]}".encode("utf8")
                        )
                    )

                elif command.type.value > common.MessageType.COMMAND.value:
                    if self.room is not None:
                        self.room.add_command(command, self)
                    else:
                        logger.warning(
                            "%s:%s - %s received but no room was joined",
                            self.address[0],
                            self.address[1],
                            command.type.value,
                        )

            try:
                while True:
                    try:
                        command = self._command_queue.get_nowait()
                    except queue.Empty:
                        break

                    logger.debug("Sending to %s:%s - %s", self.address[0], self.address[1], command.type)
                    common.write_message(self.socket, command)

                    self._command_queue.task_done()

                if self._list_all_clients_flag:
                    common.write_message(self.socket, self._server.get_list_all_clients_command())
                    self._list_all_clients_flag = False

                if self._list_rooms_flag:
                    common.write_message(self.socket, self._server.get_list_rooms_command())
                    self._list_rooms_flag = False
            except common.ClientDisconnectedException:
                break

        self._server.handle_client_disconnect(self)

    def add_command(self, command):
        self._command_queue.put(command)


class Room:
    """
    Room class is responsible for:
    - handling its list of clients (as Connection instances)
    - keep a list of commands, to be dispatched to new clients
    - dispatch added commands to already clients already in the room
    """

    def __init__(self, server: Server, room_name: str):
        self.name = room_name
        self.keep_open = False  # Should the room remain open when no more clients are inside ?
        self.byte_size = 0

        self.metadata: Dict[str, Any] = {}  # metadata are used between clients, but not by the server

        self._commands: List[common.Command] = []

        self.join_flag = False

        self._commands_mutex: threading.RLock = threading.RLock()
        self._connections: List[Connection] = []

    def client_count(self):
        return len(self._connections)

    def command_count(self):
        return len(self._commands)

    def add_client(self, connection: Connection):
        logger.info(f"Add Client {connection.address} to Room {self.name}")
        self._connections.append(connection)

    def remove_client(self, connection: Connection):
        logger.info("Remove Client % s from Room % s", connection.address, self.name)
        self._connections.remove(connection)

    def client_ids(self):
        return [c.client_id() for c in self._connections]

    def room_dict(self):
        return {
            **self.metadata,
            common.RoomMetadata.KEEP_OPEN: self.keep_open,
            common.RoomMetadata.COMMAND_COUNT: self.command_count(),
            common.RoomMetadata.BYTE_SIZE: self.byte_size,
        }

    def broadcast_commands(self, connection: Connection):
        with self._commands_mutex:
            for command in self._commands:
                connection.add_command(command)

    def add_command(self, command, sender: Connection):
        def merge_command():
            """
            Add the command to the room list, possibly merge with the previous command.
            """
            command_type = command.type
            if command_type.value > common.MessageType.OPTIMIZED_COMMANDS.value:
                command_path = common.decode_string(command.data, 0)[0]
                if len(self._commands) > 0:
                    stored_command = self._commands[-1]
                    if (
                        command_type == stored_command.type
                        and command_path == common.decode_string(stored_command.data, 0)[0]
                    ):
                        self._commands.pop()
                        self.byte_size -= stored_command.byte_size()
            self._commands.append(command)
            self.byte_size += command.byte_size()

        with self._commands_mutex:
            current_byte_size = self.byte_size
            current_command_count = len(self._commands)
            merge_command()

            room_update = {}
            if self.byte_size != current_byte_size:
                room_update[common.RoomMetadata.BYTE_SIZE] = self.byte_size
            if current_command_count != len(self._commands):
                room_update[common.RoomMetadata.COMMAND_COUNT] = len(self._commands)

            sender._server.broadcast_room_update(self, room_update)

            for connection in self._connections:
                if connection != sender:
                    connection.add_command(command)


class Server:
    def __init__(self):
        Address = Tuple[str, str]  # noqa
        self._rooms: Dict[str, Room] = {}
        # Connections not joined to any room
        self._unjoined_connections: Dict[Address, Connection] = {}
        self._mutex = threading.RLock()

    def client_count(self):
        """
        Returns (number of joined connections, number of unjoined connections)
        """
        joined = 0
        for room in self._rooms.values():
            joined += room.client_count()
        unjoined = len(self._unjoined_connections)
        return (joined, unjoined)

    def _remove_unjoined_client(self, connection: Connection):
        with self._mutex:
            logger.debug("Server : removing unjoined client %s", connection.address)
            del self._unjoined_connections[connection.address]

    def delete_room(self, room_name: str):
        with self._mutex:
            if room_name not in self._rooms:
                logger.warning("Room %s does not exist.", room_name)
                return
            if self._rooms[room_name].client_count() > 0:
                logger.warning("Room %s is not empty.", room_name)
                return
            if self._rooms[room_name].join_flag:
                logger.warning("Room %s is being joined.", room_name)
                return

            del self._rooms[room_name]
            logger.info(f"Room {room_name} deleted")

            self.broadcast_to_all_clients(
                common.Command(common.MessageType.ROOM_DELETED, common.encode_string(room_name))
            )

    def _create_room(self, connection: Connection, room_name: str):
        logger.info(f"Room {room_name} does not exist. Creating it.")
        room = Room(self, room_name)
        room.add_client(connection)
        connection.room = room
        connection.add_command(common.Command(common.MessageType.CONTENT))

        with self._mutex:
            self._rooms[room_name] = room
            logger.info(f"Room {room_name} added")

            self.broadcast_room_update(room, room.room_dict())  # Inform new room
            self.broadcast_client_update(connection, {common.ClientMetadata.ROOM: connection.room.name})

    def join_room(self, connection: Connection, room_name: str):
        assert connection.room is None

        with self._mutex:
            peer = connection.address
            if peer in self._unjoined_connections:
                logger.debug("Reusing connection %s", peer)
                del self._unjoined_connections[peer]

            room = self._rooms.get(room_name)
            if room:
                room.join_flag = True  # Room cannot be deleted from here

        if room is None:
            self._create_room(connection, room_name)
            return

        try:
            connection.room = room
            connection.add_command(common.Command(common.MessageType.CLEAR_CONTENT))
            room.broadcast_commands(connection)
            room.add_client(connection)
        except Exception as e:
            connection.room = None
            raise e
        finally:
            room.join_flag = False  # Room can be delete from here

        self.broadcast_client_update(connection, {common.ClientMetadata.ROOM: connection.room.name})

    def leave_room(self, connection: Connection, room_name: str):
        with self._mutex:
            room = self._rooms.get(room_name)
            if room is None:
                raise ValueError(f"Room not found {room_name})")
            room.remove_client(connection)

            peer = connection.address
            assert peer not in self._unjoined_connections
            self._unjoined_connections[peer] = connection
            connection.room = None

            connection.add_command(common.Command(common.MessageType.LEAVE_ROOM))
            self.broadcast_client_update(connection, {common.ClientMetadata.ROOM: None})

            if room.client_count() == 0 and not room.keep_open:
                logger.info('No more clients in room "%s" and not keep_open', room.name)
                self.delete_room(room.name)
            else:
                logger.info(f"Connections left in room {room.name}: {room.client_count()}.")

    def all_connections(self) -> List[Connection]:
        with self._mutex:
            connections = list(self._unjoined_connections.values())
            for room in self._rooms.values():
                connections += room._connections
            return connections

    def broadcast_to_all_clients(self, command: common.Command):
        with self._mutex:
            for connection in self.all_connections():
                connection.add_command(command)

    def broadcast_client_update(self, connection: Connection, metadata: Dict[str, Any]):
        if metadata == {}:
            return

        self.broadcast_to_all_clients(
            common.Command(common.MessageType.CLIENT_UPDATE, common.encode_json({connection.get_unique_id(): metadata}))
        )

    def broadcast_room_update(self, room: Room, metadata: Dict[str, Any]):
        if metadata == {}:
            return

        self.broadcast_to_all_clients(
            common.Command(common.MessageType.ROOM_UPDATE, common.encode_json({room.name: metadata}),)
        )

    def set_room_metadata(self, room_name: str, metadata: Mapping[str, Any]):
        with self._mutex:
            if room_name not in self._rooms:
                logger.warning("Room %s does not exist.", room_name)
                return

            diff = update_dict_and_get_diff(self._rooms[room_name].metadata, metadata)
            self.broadcast_room_update(self._rooms[room_name], diff)

    def set_room_keep_open(self, room_name: str, value: bool):
        with self._mutex:
            if room_name not in self._rooms:
                logger.warning("Room %s does not exist.", room_name)
                return
            room = self._rooms[room_name]
            if room.keep_open != value:
                room.keep_open = value
                self.broadcast_room_update(room, {common.RoomMetadata.KEEP_OPEN: room.keep_open})

    def get_list_rooms_command(self) -> common.Command:
        with self._mutex:
            result_dict = {
                room_name: {
                    **value.metadata,
                    common.RoomMetadata.KEEP_OPEN: value.keep_open,
                    common.RoomMetadata.COMMAND_COUNT: value.command_count(),
                    common.RoomMetadata.BYTE_SIZE: value.byte_size,
                }
                for room_name, value in self._rooms.items()
            }
            return common.Command(common.MessageType.LIST_ROOMS, common.encode_json(result_dict))

    def get_list_all_clients_command(self) -> common.Command:
        with self._mutex:
            result_dict = {connection.get_unique_id(): connection.client_id() for connection in self.all_connections()}
            return common.Command(common.MessageType.LIST_ALL_CLIENTS, common.encode_json(result_dict))

    def handle_client_disconnect(self, connection: Connection):
        if connection.room is not None:
            self.leave_room(connection, connection.room.name)

        self._remove_unjoined_client(connection)

        try:
            connection.socket.close()
        except Exception as e:
            logger.warning(e)
        logger.info("%s closed", connection.address)

        self.broadcast_to_all_clients(
            common.Command(common.MessageType.CLIENT_DISCONNECTED, common.encode_string(connection.get_unique_id()))
        )

    def run(self, port):
        global SHUTDOWN
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        binding_host = ""
        sock.bind((binding_host, port))
        sock.setblocking(0)
        sock.listen(1000)

        logger.info("Listening on port % s", port)
        while True:
            try:
                timeout = 0.1  # Check for a new client every 10th of a second
                readable, _, _ = select.select([sock], [], [], timeout)
                if len(readable) > 0:
                    client_socket, client_address = sock.accept()
                    connection = Connection(self, client_socket, client_address)
                    assert connection.address not in self._unjoined_connections
                    self._unjoined_connections[connection.address] = connection
                    connection.start()
                    logger.info(f"New connection from {client_address}")
                    self.broadcast_client_update(connection, connection.client_id())
            except KeyboardInterrupt:
                break

        logger.info("Shutting down server")
        SHUTDOWN = True
        sock.close()


def main():
    args, args_parser = parse_cli_args()
    init_logging(args)

    server = Server()
    server.run(args.port)


def parse_cli_args():
    parser = argparse.ArgumentParser(description="Start broadcasting server for Mixer")
    add_logging_cli_args(parser)
    parser.add_argument("--port", type=int, default=common.DEFAULT_PORT)
    return parser.parse_args(), parser


if __name__ == "__main__":
    main()
