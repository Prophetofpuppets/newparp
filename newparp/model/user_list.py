import json, time, uuid


class InvalidToken(Exception): pass


class ConnectionTokenStore(object):
    """
    Helper class for managing connection tokens.

    The live process doesn't do any authentication. Instead it uses a token
    generated by one of the chat endpoints.
    """
    expire_time       = 10 # is this long enough?
    forward_token_key = "connection:token:%s"
    reverse_token_key = "connection:user:%s:%s"

    def __init__(self, redis):
        self.redis = redis

    create_connection_token_script = """
        local forward = "connection:token:"..ARGV[3]
        local reverse = "connection:user:"..ARGV[1]..":"..ARGV[2]
        local existing_token = redis.call("get", reverse)
        if existing_token then
            redis.call("del", "connection:token:"..existing_token)
        end
        redis.call("hmset",  forward, "user_id", ARGV[1], "chat_id", ARGV[2], "session_id", ARGV[3])
        redis.call("expire", forward, {expire_time})
        redis.call("set",    reverse, ARGV[4])
        redis.call("expire", reverse, {expire_time})
    """.format(expire_time=expire_time)

    def create_connection_token(self, user_id, chat_id, session_id):
        """
        Creates a connection token for the user in the chat. Returns a UUID to
        be passed to the front end.
        """
        token = str(uuid.uuid4())
        self.redis.eval(self.create_connection_token_script, 0, user_id, chat_id, session_id, token)
        return token

    use_connection_token_script = """
        local user_id = redis.call("hget", "connection:token:"..ARGV[1], "user_id")
        local chat_id = redis.call("hget", "connection:token:"..ARGV[1], "chat_id")
        local chat_id = redis.call("hget", "connection:token:"..ARGV[1], "session_id")
        if not user_id then return {} end

        redis.call("del", "connection:token:"..ARGV[1], "connection:user:"..user_id..":"..chat_id)
        return {user_id, chat_id, session_id}
    """

    def use_connection_token(self, token):
        """
        Gets details for (and destroys) a token. Returns a (user_id, chat_id)
        tuple.
        """
        try:
            token_uuid = uuid.UUID(token)
        except ValueError:
            raise InvalidToken("Not a UUID.")
        data = self.redis.eval(self.use_connection_token_script, 0, token)
        if not data:
            raise InvalidToken("Token doesn't exist or already used.")
        return int(data[0]), int(data[1]), data[2]

    invalidate_connection_token_script = """
        local reverse = "connection:user:"..ARGV[1]..":"..ARGV[2]
        local token = redis.call("get", reverse)
        if not token then return end

        redis.call("del", reverse, "connection:token:"..token)
    """

    def invalidate_connection_token(self, user_id, chat_id):
        """
        Invalidates a token by user_id and chat_id. For when a user is banned or
        uninvited from a chat and we want to make sure they can't use a token
        from before.
        """
        self.redis.eval(self.invalidate_connection_token_script, 0, user_id, chat_id)

    def invalidate_all_tokens_for_user(self, user_id):
        """
        Invalidates all of a user's tokens. For when a user is deactivated or
        given a site-wide ban.
        """
        next_index = 0
        pattern = "connection:user:%s:*" % user_id
        while True:
            next_index, keys = redis.scan(next_index, pattern)
            for key in keys:
                token = self.redis.get(key)
                if not token:
                    continue
                self.redis.delete(self.forward_token_key % token)
            if next_index == 0:
                break


class UserListStore(object):
    """
    Helper class for managing online state.

    Redis keys used for online state:
    * chat:<chat_id>:online - map of socket ids -> user ids
    * chat:<chat_id>:online:<socket_id> - string, with the session id that the
      socket belongs to. Has a TTL to allow reaping.
    * chat:<chat_id>:typing - set, with user numbers of people who are typing.
    """

    @classmethod
    def scan_active_chats(cls, redis):
        """
        Returns an iterator of all the chat IDs where someone is online.
        """
        next_index = 0
        while True:
            next_index, keys = redis.scan(next_index, "chat:*:online")
            for key in keys:
                yield int(key[5:-7])
            if next_index == 0:
                break

    def __init__(self, redis, chat_id):
        self.redis   = redis
        self.chat_id = chat_id
        self.online_key  = "chat:%s:online"     % self.chat_id
        self.session_key = "chat:%s:online:%%s" % self.chat_id
        self.typing_key  = "chat:%s:typing"     % self.chat_id

    def socket_join(self, socket_id, session_id, user_id):
        """
        Joins a socket to a chat. Returns a boolean indicating whether or not
        the user's online state changed.
        """
        pipe = self.redis.pipeline()

        # Remember whether they're already online.
        pipe.hvals(self.online_key)

        # Queue their last_online update.
        # TODO make sure celery is reading this from the right redis instance
        pipe.hset("queue:usermeta", "chatuser:%s" % user_id, json.dumps({
            "last_online": str(time.time()),
            "chat_id": self.chat_id,
        }))

        # Add them to the online list.
        pipe.hset(self.online_key, socket_id, user_id)
        pipe.setex(self.session_key % socket_id, 30, session_id)

        result = pipe.execute()

        return str(user_id) not in result[0]

    socket_ping_script = """
        local user_id_from_chat = redis.call("hget", "chat:"..ARGV[1]..":online", ARGV[2])
        if not user_id_from_chat then return false end

        local session_id = redis.call("get", "chat:"..ARGV[1]..":online:"..ARGV[2])
        if not session_id then return false end

        redis.call("expire", "chat:"..ARGV[1]..":online:"..ARGV[2], 30)
        return true
    """

    def socket_ping(self, socket_id):
        """
        Bumps a socket's ping time to avoid timeouts. This raises
        PingTimeoutException if they've already timed out.
        """
        result = self.redis.eval(self.socket_ping_script, 0, self.chat_id, socket_id)
        if not result:
            raise PingTimeoutException

    def socket_disconnect(self, socket_id, user_number):
        """
        Removes a socket from a chat. Returns a boolean indicating whether the
        user's online state has changed.
        """
        pipe = self.redis.pipeline()
        pipe.hget(self.online_key, socket_id)
        pipe.hdel(self.online_key, socket_id)
        pipe.delete(self.session_key % socket_id)
        pipe.srem(self.typing_key, user_number)
        pipe.hvals(self.online_key)
        user_id, _, _, _, new_user_ids = pipe.execute()
        if not user_id:
            return False
        return user_id not in new_user_ids

    user_disconnect_script = """
        local had_online_socket = false
        local online_list = redis.call("hgetall", "chat:"..ARGV[1]..":online")
        if #online_list == 0 then return false end
        for i = 1, #online_list, 2 do
            local socket_id = online_list[i]
            local user_id = online_list[i+1]
            if user_id == ARGV[2] then
                redis.call("hdel", "chat:"..ARGV[1]..":online", socket_id)
                redis.call("del",  "chat:"..ARGV[1]..":online:"..socket_id)
                had_online_socket = true
            end
        end
        redis.call("srem", "chat:"..ARGV[1]..":typing", ARGV[3])
        return had_online_socket
    """

    def user_disconnect(self, user_id, user_number):
        """
        Removes all of a user's sockets from a chat. Returns a boolean
        indicating whether the user's online state has changed.
        """
        result = self.redis.eval(self.user_disconnect_script, 0, self.chat_id, user_id, user_number)
        return bool(result)

    def user_ids_online(self):
        """Returns a set of user IDs who are online."""
        return set(int(_) for _ in self.redis.hvals(self.online_key))

    @classmethod
    def multi_user_ids_online(cls, redis, chat_ids):
        """
        Returns a set of user IDs who are online in many chats.
        """
        pipe = redis.pipeline()
        for chat_id in chat_ids:
            pipe.hvals("chat:%s:online" % chat_id)
        return (set(int(user_id) for user_id in chat) for chat in pipe.execute())

    session_has_open_socket_script = """
        local online_list = redis.call("hgetall", "chat:"..ARGV[1]..":online")
        if #online_list == 0 then return false end
        for i = 1, #online_list, 2 do
            local socket_id = online_list[i]
            local user_id = online_list[i+1]
            local session_id = redis.call("get", "chat:"..ARGV[1]..":online:"..socket_id)
            if session_id == ARGV[2] then
                if user_id == ARGV[3] then
                    return true
                end
                return false
            end
        end
        return false
    """

    def session_has_open_socket(self, session_id, user_id):
        """
        Indicates whether there's an open socket matching the session ID and
        user ID.
        """
        result = self.redis.eval(self.session_has_open_socket_script, 0, self.chat_id, session_id, user_id)
        return bool(result)

    def user_start_typing(self, user_number):
        """
        Mark a user as typing. Returns a bool indicating whether the user's
        typing state has changed.
        """
        return bool(self.redis.sadd(self.typing_key, user_number))

    def user_stop_typing(self, user_number):
        """
        Mark a user as no longer typing. Returns a bool indicating whether the
        user's typing state has changed.
        """
        return bool(self.redis.srem(self.typing_key, user_number))

    def user_numbers_typing(self):
        """Returns a list of user numbers who are typing."""
        return list(int(_) for _ in self.redis.smembers(self.typing_key))

    inconsistent_entries_script = """
        local online_list = redis.call("hgetall", "chat:"..ARGV[1]..":online")
        if #online_list == 0 then return {} end

        local inconsistent_entries = {}

        for i = 1, #online_list, 2 do
            local socket_id = online_list[i]
            local user_id = online_list[i+1]
            if redis.call("exists", "chat:"..ARGV[1]..":online:"..socket_id) == 0 then
                table.insert(inconsistent_entries, {socket_id, user_id})
            end
        end

        return inconsistent_entries
    """

    def inconsistent_entries(self):
        """
        Returns a list of socket_id/user_id pairs where the sockets have
        expired.
        """
        return [
            (_[0], int(_[1]))
            for _ in self.redis.eval(self.inconsistent_entries_script, 0, self.chat_id)
        ]

    # TODO manage kicking here too?


class PingTimeoutException(Exception): pass
