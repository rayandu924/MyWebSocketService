#!/usr/bin/env python3
import asyncio
import json
import logging
import uuid
import app.server as server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("websocket-room-server")

class WebSocketRoomServer:
    def __init__(self, host="0.0.0.0", port=8080):
        self.host = host
        self.port = port

        # Dictionnaire { connection_id: websocket_object }
        self.connections = {}

        # Dictionnaire { room_name: set(connection_ids) }
        self.rooms = {}

        # Mappe un "type" de message à la fonction qui le gère
        self.message_handlers = {
            "join_room": self.handle_join_room,
            "leave_room": self.handle_leave_room,
            "send_room": self.handle_send_room,
            "send_user": self.handle_send_user,
            "get_info": self.handle_get_info
        }

    async def handler(self, websocket):
        """
        Fonction appelée à chaque connexion WebSocket entrante.
        """
        connection_id = str(uuid.uuid4())
        self.connections[connection_id] = websocket

        logger.info(f"[SERVER] Nouvelle connexion : {connection_id}")
        # Envoyer l'ID de connexion au client
        await self.send_json(websocket, {
            "type": "connection_id",
            "connection_id": connection_id
        })

        try:
            async for message in websocket:
                # Tenter de parser le JSON
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    await self.send_error(websocket, "JSON invalide")
                    continue

                # Récupérer le type de message et trouver le handler
                msg_type = data.get("type")
                handler = self.message_handlers.get(msg_type)

                if handler is not None:
                    await handler(connection_id, data)
                else:
                    await self.send_error(websocket, f"Type de message inconnu: {msg_type}")

        except server.exceptions.ConnectionClosedError as e:
            logger.warning(f"[SERVER] Connexion fermée de manière inattendue : {connection_id} ({e})")
        except Exception as e:
            logger.error(f"[SERVER] Erreur dans handler() : {e}")

        finally:
            # Nettoyage de la connexion
            await self.cleanup_connection(connection_id)
            logger.info(f"[SERVER] Connexion fermée : {connection_id}")

    async def send_json(self, websocket, payload):
        """Envoie un dictionnaire Python sérialisé en JSON sur une websocket."""
        try:
            await websocket.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"[SERVER] Erreur lors de l'envoi JSON : {e}")

    async def send_to(self, connection_id, payload):
        """
        Envoie un payload JSON à la connexion spécifiée.
        """
        ws = self.connections.get(connection_id)
        if ws is None:
            logger.warning(f"[SERVER] 'send_to' cible inconnue: {connection_id}")
            return
        await self.send_json(ws, payload)

    async def broadcast_to_room(self, room_name, payload):
        """
        Diffuse un payload JSON à tous les membres d'une salle.
        """
        members = self.rooms.get(room_name, set())
        for member_id in list(members):
            await self.send_to(member_id, payload)

    async def handle_join_room(self, connection_id, data):
        room_name = data.get("room")
        if not room_name:
            return await self.send_error_to(connection_id, "Nom de salle requis")

        # Ajout de la salle si inexistante
        self.rooms.setdefault(room_name, set()).add(connection_id)
        logger.info(f"[SERVER] {connection_id} rejoint la salle : {room_name}")

        # Notifier les autres membres
        await self.broadcast_to_room(room_name, {
            "type": "room_info",
            "event": "user_joined",
            "userId": connection_id,
            "room": room_name
        })

        # Confirmer au client qu'il a rejoint
        await self.send_to(connection_id, {
            "type": "joined_room",
            "room": room_name
        })

    async def handle_leave_room(self, connection_id, data):
        room_name = data.get("room")
        if not room_name:
            return await self.send_error_to(connection_id, "Nom de salle requis")

        if room_name not in self.rooms or connection_id not in self.rooms[room_name]:
            return await self.send_error_to(connection_id, "Vous n'êtes pas dans cette salle")

        # Retirer l'utilisateur
        self.rooms[room_name].remove(connection_id)
        logger.info(f"[SERVER] {connection_id} a quitté la salle : {room_name}")

        # Informer les autres
        await self.broadcast_to_room(room_name, {
            "type": "room_info",
            "event": "user_left",
            "userId": connection_id,
            "room": room_name
        })

        # Confirmer au client
        await self.send_to(connection_id, {
            "type": "left_room",
            "room": room_name
        })

        # Supprimer la salle si vide
        if not self.rooms[room_name]:
            del self.rooms[room_name]

    async def handle_send_room(self, connection_id, data):
        room_name = data.get("room")
        payload = data.get("payload", {})
        if not room_name:
            return await self.send_error_to(connection_id, "Nom de salle requis")

        if room_name not in self.rooms or connection_id not in self.rooms[room_name]:
            return await self.send_error_to(connection_id, "Vous n'êtes pas dans cette salle")

        broadcast_msg = {
            "type": "room_message",
            "room": room_name,
            "from": connection_id,
            "payload": payload
        }
        await self.broadcast_to_room(room_name, broadcast_msg)

    async def handle_send_user(self, connection_id, data):
        target_id = data.get("to")
        payload = data.get("payload", {})
        if not target_id:
            return await self.send_error_to(connection_id, "'to' manquant pour send_user")

        if target_id not in self.connections:
            return await self.send_error_to(connection_id, "L'utilisateur cible n'existe pas (ou plus)")

        forward_msg = {
            "type": "user_message",
            "from": connection_id,
            "payload": payload
        }
        await self.send_to(target_id, forward_msg)

    async def handle_get_info(self, connection_id, data):
        room_name = data.get("room")
        info_type = data.get("info")
        if not room_name:
            return await self.send_error_to(connection_id, "Nom de salle requis")
        if room_name not in self.rooms or connection_id not in self.rooms[room_name]:
            return await self.send_error_to(connection_id, "Vous n'êtes pas dans cette salle")
        if not info_type:
            return await self.send_error_to(connection_id, "Type d'information requis")

        if info_type == "users":
            await self.send_room_users(connection_id, room_name)
        else:
            await self.send_error_to(connection_id, f"Type d'information inconnu: {info_type}")

    async def send_room_users(self, connection_id, room_name):
        user_list = list(self.rooms[room_name])
        await self.send_to(connection_id, {
            "type": "room_info",
            "event": "current_users",
            "room": room_name,
            "users": user_list
        })

    async def send_error(self, websocket, message):
        """Envoie un message d'erreur à un websocket donné."""
        await self.send_json(websocket, {
            "type": "error",
            "message": message
        })

    async def send_error_to(self, connection_id, message):
        """Envoie un message d'erreur à un client (via connection_id)."""
        ws = self.connections.get(connection_id)
        if ws:
            await self.send_error(ws, message)

    async def cleanup_connection(self, connection_id):
        """Nettoie la connexion, retire le user de toutes les rooms, informe les autres."""
        self.connections.pop(connection_id, None)

        rooms_to_delete = []
        for room_name, members in self.rooms.items():
            if connection_id in members:
                members.remove(connection_id)
                # Informer les autres
                await self.broadcast_to_room(room_name, {
                    "type": "room_info",
                    "event": "user_left",
                    "userId": connection_id,
                    "room": room_name
                })
                if not members:
                    rooms_to_delete.append(room_name)

        # Supprime les salles vides
        for room_name in rooms_to_delete:
            del self.rooms[room_name]

    def run(self):
        """
        Lance le serveur WebSocket avec une boucle asyncio.
        """
        async def start_server():
            logger.info(f"[SERVER] Lancement du serveur sur ws://{self.host}:{self.port}")
            async with server.serve(self.handler, self.host, self.port):
                await asyncio.Future()  # Garde le serveur actif

        asyncio.run(start_server())

def main():
    server = WebSocketRoomServer()
    server.run()
if __name__ == "__main__":
    main()
