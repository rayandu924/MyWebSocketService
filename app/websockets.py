import asyncio
import json
import logging
import uuid
from aiohttp import web

# ========================================
# Config Logging
# ========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("websocket-room-server")

# ========================================
# Data Structures
# ========================================
# connections[connection_id] = WebSocket (clé = UUID généré côté serveur)
connections = {}
# rooms[room_name] = set(connection_ids)
rooms = {}

# ========================================
# Diffuse la liste des salles actives
# ========================================
async def broadcast_rooms():
    # On prépare un dict { "room_name": ["conn_id1", "conn_id2", ... ], ... }
    active_rooms = {room: list(members) for room, members in rooms.items()}
    logger.info(f"[SERVER] Salles Actives: {active_rooms}")
    message = {"type": "active_rooms", "rooms": active_rooms}

    # On envoie ce message à TOUTES les connexions connues
    for ws in connections.values():
        try:
            await ws.send_json(message)
        except Exception as e:
            logger.error(f"[SERVER] Erreur lors de la diffusion des salles: {e}")

# ========================================
# Handlers pour les types de messages
# ========================================
async def handle_create_room(connection_id, data, ws):
    room_name = data.get("room")
    if not room_name:
        await ws.send_json({"type": "error", "message": "Nom de salle requis"})
        return

    if room_name in rooms:
        await ws.send_json({"type": "error", "message": "La salle existe déjà"})
        return

    # Création de la salle
    rooms[room_name] = set()
    logger.info(f"[SERVER] Salle créée : {room_name} par {connection_id}")

    # Informer le client (le créateur de la salle)
    await ws.send_json({"type": "room_created", "room": room_name})
    # Diffuser la mise à jour des salles
    await broadcast_rooms()

async def handle_join_room(connection_id, data, ws):
    room_name = data.get("room")
    if not room_name:
        await ws.send_json({"type": "error", "message": "Nom de salle requis"})
        return

    if room_name not in rooms:
        await ws.send_json({"type": "error", "message": "La salle n'existe pas"})
        return

    # IMPORTANT : on ajoute le *connection_id* généré par le serveur
    rooms[room_name].add(connection_id)
    logger.info(f"[SERVER] {connection_id} a rejoint la salle : {room_name}")

    await ws.send_json({"type": "joined_room", "room": room_name})
    await broadcast_rooms()

async def handle_leave_room(connection_id, data, ws):
    room_name = data.get("room")
    if not room_name:
        await ws.send_json({"type": "error", "message": "Nom de salle requis"})
        return

    # Vérifier si la salle existe et si le client y est
    if room_name not in rooms or connection_id not in rooms[room_name]:
        await ws.send_json({"type": "error", "message": "Vous n'êtes pas dans cette salle"})
        return

    # On retire le client de la salle
    rooms[room_name].remove(connection_id)
    logger.info(f"[SERVER] {connection_id} a quitté la salle : {room_name}")

    await ws.send_json({"type": "left_room", "room": room_name})
    await broadcast_rooms()

async def handle_send_room(connection_id, data, ws):
    room_name = data.get("room")
    message = data.get("message")

    # Vérification de base
    if not room_name or not message:
        await ws.send_json({"type": "error", "message": "Nom de salle et message requis"})
        return

    # Vérifier que la salle existe et que l'envoyeur en fait partie
    if room_name not in rooms or connection_id not in rooms[room_name]:
        await ws.send_json({"type": "error", "message": "Vous n'êtes pas dans cette salle"})
        return

    # Préparer le message à diffuser
    broadcast_message = {
        "type": "room_message",
        "room": room_name,
        "source": connection_id,  # on peut aussi mettre un pseudo si on gère ça ailleurs
        "message": message
    }

    # On fait une copie pour éviter les modifications concurrentes
    members = rooms[room_name].copy()
    logger.info(f"[SERVER] Diffusion du message dans la salle {room_name} par {connection_id} à {members}")

    # Envoi à chaque membre présent dans la salle
    for member_id in members:
        logger.info(f"[SERVER] Envoi du message à {member_id}")
        logger.debug(f"[SERVER] Connexions Actives: {connections}")
        logger.debug(f"[SERVER] Rooms Actives: {rooms}")
        member_ws = connections.get(member_id)
        logger.debug(f"[SERVER] Member WS: {member_ws}")
        if member_ws:
            try:
                await member_ws.send_json(broadcast_message)
                logger.info(f"[SERVER] Message envoyé à {member_id}")
            except Exception as e:
                logger.error(f"[SERVER] Erreur lors de l'envoi du message à {member_id}: {e}")

    logger.info(f"[SERVER] Message diffusé dans la salle {room_name} par {connection_id}")

# Nouveau handler pour envoyer des messages privés
async def handle_send_user(connection_id, data, ws):
    target_id = data.get("target_id")
    message = data.get("message")

    # Vérifications de base
    if not target_id or not message:
        await ws.send_json({"type": "error", "message": "target_id et message requis"})
        return

    # Vérifier que l'utilisateur cible est connecté
    target_ws = connections.get(target_id)
    if not target_ws:
        await ws.send_json({"type": "error", "message": "Utilisateur cible non connecté"})
        return

    # Préparer le message à envoyer
    user_message = {
        "type": "private_message",
        "source": connection_id,
        "message": message
    }

    # Envoyer le message à l'utilisateur cible
    try:
        await target_ws.send_json(user_message)
        await ws.send_json({"type": "info", "message": f"Message envoyé à {target_id}"})
        logger.info(f"[SERVER] Message privé de {connection_id} à {target_id}: {message}")
    except Exception as e:
        logger.error(f"[SERVER] Erreur lors de l'envoi du message privé à {target_id}: {e}")
        await ws.send_json({"type": "error", "message": "Erreur lors de l'envoi du message"})

# ========================================
# WebSocket Handler principal
# ========================================
async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    # Préparation de la réponse WebSocket
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Génération d'un ID unique côté serveur
    connection_id = str(uuid.uuid4())
    # On stocke ws dans connections[connection_id]
    connections[connection_id] = ws

    logger.info(f"[SERVER] Connexion WebSocket établie : {connection_id}")
    logger.info(f"[SERVER] Connexions Actives: {list(connections.keys())}")

    try:
        # Envoyer l'ID de connexion au client pour qu'il sache lequel utiliser
        await ws.send_json({"type": "connection_id", "connection_id": connection_id})

        # Diffuser la liste mise à jour des salles
        await broadcast_rooms()

        # Écouter les messages reçus sur cette WebSocket
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    logger.info(f"[SERVER] Message reçu de {connection_id} : {data}")

                    msg_type = data.get("type")

                    if msg_type == "create_room":
                        await handle_create_room(connection_id, data, ws)
                    elif msg_type == "join_room":
                        await handle_join_room(connection_id, data, ws)
                    elif msg_type == "leave_room":
                        await handle_leave_room(connection_id, data, ws)
                    elif msg_type == "send_room":
                        await handle_send_room(connection_id, data, ws)
                    elif msg_type == "send_user":
                        await handle_send_user(connection_id, data, ws)
                    else:
                        await ws.send_json({"type": "error", "message": "Type de message inconnu"})
                        logger.warning(f"[SERVER] Type de message inconnu de {connection_id} : {msg_type}")

                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "JSON invalide"})
                    logger.error(f"[SERVER] JSON invalide reçu de {connection_id} : {msg.data}")

            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"[SERVER] Connexion {connection_id} fermée avec erreur : {ws.exception()}")

    except Exception as e:
        logger.error(f"[SERVER] Erreur dans websocket_handler : {e}")

    finally:
        # Nettoyage quand la connexion se termine (ou plante)
        connections.pop(connection_id, None)

        # Retirer la connexion de toutes les salles
        for room, members in rooms.items():
            if connection_id in members:
                members.remove(connection_id)
                logger.info(f"[SERVER] {connection_id} retiré de la salle {room}")

        logger.info(f"[SERVER] Connexion fermée : {connection_id}")
        logger.info(f"[SERVER] Connexions Actives: {list(connections.keys())}")

        # Diffuser la liste mise à jour des salles
        await broadcast_rooms()

    return ws

# ========================================
# Route Setup
# ========================================
def setup_routes(app: web.Application) -> None:
    app.router.add_get("/ws", websocket_handler)

# ========================================
# Main
# ========================================
def main():
    app = web.Application()
    setup_routes(app)

    logger.info("[SERVER] Démarrage du serveur WebSocket avec gestion de salles")
    web.run_app(app, host="0.0.0.0", port=8080)

if __name__ == "__main__":
    main()
