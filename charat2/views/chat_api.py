import json

from flask import abort, g, jsonify, make_response, request

from charat2.helpers.chat import (
    mark_alive,
    send_message,
    disconnect,
    get_userlist,
)
from charat2.model import case_options, Message
from charat2.model.connections import (
    get_user_chat,
    use_db_chat,
    db_commit,
    db_disconnect,
)
from charat2.model.validators import color_validator

@mark_alive
def messages():

	# XXX GET THIS FROM REDIS INSTEAD
    # Look for messages in the database first, and only subscribe if there
    # aren't any.
    #message_query = g.db.query(Message).filter(Message.chat_id == g.chat.id)
    #if "after" in request.form:
    #    after = int(request.form["after"])
    #    message_query = message_query.filter(Message.id > after)
    # Order descending to limit it to the last 50 messages.
    #message_query = message_query.order_by(Message.id.desc()).limit(50)

    #messages = message_query.all()
    #if len(messages) != 0:
    #    messages.reverse()
    #    return jsonify({
    #        "messages": [_.to_dict() for _ in messages],
    #        "users": get_userlist(g.db, g.redis, g.chat),
    #    })

    pubsub = g.redis.pubsub()
    # Channel for general chat messages.
    pubsub.subscribe("channel:%s" % g.chat_id)
    # Channel for messages aimed specifically at you - kicks, bans etc.
    pubsub.subscribe("channel:%s:%s" % (g.chat_id, g.user_id))

    # Get rid of the database connection here so we're not hanging onto it
    # while waiting for the redis message.
    db_commit()
    db_disconnect()

    for msg in pubsub.listen():
        if msg["type"]=="message":
            # The pubsub channel sends us a JSON string, so we return that
            # instead of using jsonify.
            resp = make_response(msg["data"])
            resp.headers["Content-type"] = "application/json"
            return resp

@mark_alive
def ping():
    return "", 204

@use_db_chat
@mark_alive
def send():

    if "text" not in request.form:
        abort(400)

    text = request.form["text"].strip()
    if text == "":
        abort(400)

    message_type = "ic"
    # Automatic OOC detection
    if (
        text.startswith("((") or text.endswith("))")
        or text.startswith("[[") or text.endswith("]]")
        or text.startswith("{{") or text.endswith("}}")
    ):
        message_type="ooc"

    send_message(g.db, g.redis, Message(
        chat_id=g.chat.id,
        user_id=g.user.id,
        type=message_type,
        color=g.user_chat.color,
        acronym=g.user_chat.acronym,
        text=text,
    ))

    return "", 204

@mark_alive
def set_state():
    raise NotImplementedError

@mark_alive
def set_group():
    raise NotImplementedError

@mark_alive
def user_action():
    raise NotImplementedError

@mark_alive
def set_flag():
    raise NotImplementedError

@mark_alive
def set_info():
    raise NotImplementedError

@use_db_chat
@mark_alive
def save():

    # Remember old values so we can check if they've changed later.
    old_name = g.user_chat.name
    old_acronym = g.user_chat.acronym
    old_color = g.user_chat.color

    # Don't allow a blank name.
    if request.form["name"]=="":
        abort(400)

    # Validate color.
    if not color_validator.match(request.form["color"]):
        abort(400)
    g.user_chat.color = request.form["color"]

    # Validate case.
    if request.form["case"] not in case_options.enums:
        abort(400)
    g.user_chat.case = request.form["case"]

    # There are length limits on the front end so just silently truncate these.
    g.user_chat.name = request.form["name"][:50]
    g.user_chat.acronym = request.form["acronym"][:15]
    g.user_chat.quirk_prefix = request.form["quirk_prefix"][:50]
    g.user_chat.quirk_suffix = request.form["quirk_suffix"][:50]

    # XXX PUT LENGTH LIMIT ON REPLACEMENTS?
    # Zip replacements.
    replacements = zip(
        request.form.getlist("quirk_from"),
        request.form.getlist("quirk_to")
    )
    # Strip out any rows where from is blank or the same as to.
    replacements = [_ for _ in replacements if _[0]!="" and _[0]!=_[1]]
    # And encode as JSON.
    g.user_chat.replacements = json.dumps(replacements)

    # Send a message if name or acronym has changed.
    if (
        g.user_chat.group!="silent"
        and (g.user_chat.name!=old_name or g.user_chat.acronym!=old_acronym)
    ):
        send_message(g.db, g.redis, Message(
            chat_id=g.chat.id,
            type="user_info",
            text="%s [%s] is now %s [%s]." % (
                old_name, old_acronym,
                g.user_chat.name, g.user_chat.acronym,
            ),
        ))

    return "", 204

def quit():
    # Only send the message if we were already online.
    if g.user_id is None or "chat_id" not in request.form:
        abort(400)
    try:
        g.chat_id = int(request.form["chat_id"])
    except ValueError:
        abort(400)
    if disconnect(g.redis, g.chat_id, g.user_id):
        get_user_chat()
        send_message(g.db, g.redis, Message(
            chat_id=g.chat.id,
            type="disconnect",
            text="%s [%s] disconnected." % (
                g.user_chat.name, g.user_chat.acronym,
            ),
        ))
    return "", 204

