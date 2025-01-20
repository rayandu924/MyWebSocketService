#!/usr/bin/env python3
import asyncio
import json
import logging
import uuid
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("websocket-room-server")

connections = {}  # { connection_id: websocket }
rooms = {}        # { room_name: set of connection_ids }

async def broadcast_rooms():
    """Diffuser la liste des salles (optionnel)."""
    active_rooms = {room: list(members) for room, members in rooms.items()}
    message = {"type": "active_rooms", "rooms": active_rooms}
    for ws in connections.values():
        try:
            await ws.send_json(message)
        except:
            pass

async def send_to(connection_id, payload):
    """Envoie un payload JSON à une connexion donnée."""
    ws = connections.get(connection_id)
    if ws is None:
        logger.warning(f"[SERVER] 'send_to' cible inconnue: {connection_id}")
        return
    try:
        await ws.send_json(payload)
    except Exception as e:
        logger.error(f"[SERVER] Erreur lors de l'envoi à {connection_id}: {e}")

async def broadcast_to_room(room_name, payload):
    """Diffuser un payload JSON à tous les membres d'une salle."""
    if room_name not in rooms:
        return
    for member_id in rooms[room_name].copy():
        await send_to(member_id, payload)

# ---------------------------------------------------------------------
# NOUVEAU : seulement 4 handlers (join_room / leave_room / send_room / send_user)
# ---------------------------------------------------------------------

async def handle_join_room(connection_id, data, ws):
    """
    data = { "type": "join_room", "room": "nomDeLaRoom" }
    """
    room_name = data.get("room")
    if not room_name:
        await ws.send_json({"type": "error", "message": "Nom de salle requis"})
        return

    # Si la salle n'existe pas, on la crée
    if room_name not in rooms:
        rooms[room_name] = set()

    # Notifier les autres membres de la salle de l'arrivée d'un nouveau (si besoin)
    for member_id in rooms[room_name]:
        await send_to(member_id, {
            "type": "room_info",
            "event": "user_joined",
            "userId": connection_id,
            "room": room_name
        })

    # Ajouter le nouvel arrivant
    rooms[room_name].add(connection_id)
    logger.info(f"[SERVER] {connection_id} rejoint la salle: {room_name}")

    # Confirmer au client
    await ws.send_json({"type": "joined_room", "room": room_name})
    await broadcast_rooms()

async def handle_leave_room(connection_id, data, ws):
    """
    data = { "type": "leave_room", "room": "nomDeLaRoom" }
    """
    room_name = data.get("room")
    if not room_name:
        await ws.send_json({"type": "error", "message": "Nom de salle requis"})
        return

    if room_name not in rooms or connection_id not in rooms[room_name]:
        await ws.send_json({"type": "error", "message": "Vous n'êtes pas dans cette salle"})
        return

    # Retirer l'utilisateur de la salle
    rooms[room_name].remove(connection_id)
    logger.info(f"[SERVER] {connection_id} a quitté la salle: {room_name}")

    # Informer les autres membres de la salle
    await broadcast_to_room(room_name, {
        "type": "room_info",
        "event": "user_left",
        "userId": connection_id,
        "room": room_name
    })

    # Confirmer au client
    await ws.send_json({"type": "left_room", "room": room_name})
    await broadcast_rooms()

async def handle_send_room(connection_id, data, ws):
    """
    data = {
      "type": "send_room",
      "room": "<roomName>",
      "payload": { ... }
    }
    """
    room_name = data.get("room")
    if not room_name:
        await ws.send_json({"type": "error", "message": "Nom de salle requis"})
        return
    if room_name not in rooms or connection_id not in rooms[room_name]:
        await ws.send_json({"type": "error", "message": "Vous n'êtes pas dans cette salle"})
        return

    # Préparer le message à diffuser
    broadcast_msg = {
        "type": "room_message",
        "room": room_name,
        "from": connection_id,
        "payload": data.get("payload", {})
    }
    await broadcast_to_room(room_name, broadcast_msg)

async def handle_send_user(connection_id, data, ws):
    """
    data = {
      "type": "send_user",
      "to": "<targetConnectionId>",
      "payload": { ... }
    }

    => On relaie simplement le contenu de 'payload' à l'utilisateur cible,
       en y ajoutant 'from' pour que le destinataire sache qui envoie.
    """
    target_id = data.get("to")
    if not target_id:
        await ws.send_json({"type": "error", "message": "'to' manquant pour send_user"})
        return
    if target_id not in connections:
        await ws.send_json({"type": "error", "message": "L'utilisateur cible n'existe pas (ou plus)"})
        return

    # On prépare le message à envoyer au destinataire
    forward_msg = {
        "type": "user_message",
        "from": connection_id,
        "payload": data.get("payload", {})
    }
    await send_to(target_id, forward_msg)

# ---------------------------------------------------------------------
# WebSocket handler principal
# ---------------------------------------------------------------------
async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    connection_id = str(uuid.uuid4())
    connections[connection_id] = ws

    logger.info(f"[SERVER] Connexion WebSocket établie : {connection_id}")
    # Envoyer l'ID de connexion au client
    await ws.send_json({"type": "connection_id", "connection_id": connection_id})
    await broadcast_rooms()

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get("type")

                    if msg_type == "join_room":
                        await handle_join_room(connection_id, data, ws)
                    elif msg_type == "leave_room":
                        await handle_leave_room(connection_id, data, ws)
                    elif msg_type == "send_room":
                        await handle_send_room(connection_id, data, ws)
                    elif msg_type == "send_user":
                        await handle_send_user(connection_id, data, ws)
                    else:
                        await ws.send_json({
                            "type": "error",
                            "message": f"Type de message inconnu: {msg_type}"
                        })

                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "JSON invalide"})
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"[SERVER] Conn {connection_id} fermée avec erreur: {ws.exception()}")

    except Exception as e:
        logger.error(f"[SERVER] Erreur websocket_handler : {e}")

    finally:
        # En cas de déconnexion
        connections.pop(connection_id, None)
        # Enlever la connexion de toutes les rooms
        for room_id, members in rooms.items():
            if connection_id in members:
                members.remove(connection_id)
                # Informer les autres
                for other_id in members.copy():
                    await send_to(other_id, {
                        "type": "room_info",
                        "event": "user_left",
                        "userId": connection_id,
                        "room": room_id
                    })
        logger.info(f"[SERVER] Connexion fermée : {connection_id}")
        await broadcast_rooms()

    return ws

def setup_routes(app: web.Application) -> None:
    app.router.add_get("/ws", websocket_handler)

def main():
    app = web.Application()
    setup_routes(app)
    logger.info("[SERVER] Serveur démarré sur port 8080")
    web.run_app(app, host="0.0.0.0", port=8080)

if __name__ == "__main__":
    main()
