#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CQHTTP (OneBot) v11 协议适配
============================

协议详情请看: `CQHTTP`_ | `OneBot`_

.. _CQHTTP:
    http://cqhttp.cc/
.. _OneBot:
    https://github.com/howmanybots/onebot
"""

import re
import sys
import asyncio

import httpx

from nonebot.log import logger
from nonebot.config import Config
from nonebot.message import handle_event
from nonebot.typing import Any, Dict, Union, Tuple, Iterable, Optional
from nonebot.exception import NetworkError, ActionFailed, ApiNotAvailable
from nonebot.typing import overrides, Driver, WebSocket, NoReturn
from nonebot.adapters import BaseBot, BaseEvent, BaseMessage, BaseMessageSegment


def escape(s: str, *, escape_comma: bool = True) -> str:
    """
    对字符串进行 CQ 码转义。

    ``escape_comma`` 参数控制是否转义逗号（``,``）。
    """
    s = s.replace("&", "&amp;") \
        .replace("[", "&#91;") \
        .replace("]", "&#93;")
    if escape_comma:
        s = s.replace(",", "&#44;")
    return s


def unescape(s: str) -> str:
    """对字符串进行 CQ 码去转义。"""
    return s.replace("&#44;", ",") \
        .replace("&#91;", "[") \
        .replace("&#93;", "]") \
        .replace("&amp;", "&")


def _b2s(b: Optional[bool]) -> Optional[str]:
    return b if b is None else str(b).lower()


def _check_at_me(bot: "Bot", event: "Event"):
    if event.type != "message":
        return

    if event.detail_type == "private":
        event.to_me = True
    else:
        event.to_me = False
        at_me_seg = MessageSegment.at(event.self_id)

        # check the first segment
        first_msg_seg = event.message[0]
        if first_msg_seg == at_me_seg:
            event.to_me = True
            del event.message[0]

        if not event.to_me:
            # check the last segment
            i = -1
            last_msg_seg = event.message[i]
            if last_msg_seg.type == "text" and \
                    not last_msg_seg.data["text"].strip() and \
                    len(event.message) >= 2:
                i -= 1
                last_msg_seg = event.message[i]

            if last_msg_seg == at_me_seg:
                event.to_me = True
                del event.message[i:]

        if not event.message:
            event.message.append(MessageSegment.text(""))


def _check_nickname(bot: "Bot", event: "Event"):
    if event.type != "message":
        return

    first_msg_seg = event.message[0]
    if first_msg_seg.type != "text":
        return

    first_text = first_msg_seg.data["text"]

    if bot.config.NICKNAME:
        # check if the user is calling me with my nickname
        if isinstance(bot.config.NICKNAME, str) or \
                not isinstance(bot.config.NICKNAME, Iterable):
            nicknames = (bot.config.NICKNAME,)
        else:
            nicknames = filter(lambda n: n, bot.config.NICKNAME)
        nickname_regex = "|".join(nicknames)
        m = re.search(rf"^({nickname_regex})([\s,，]*|$)", first_text,
                      re.IGNORECASE)
        if m:
            nickname = m.group(1)
            logger.debug(f"User is calling me {nickname}")
            event.to_me = True
            first_msg_seg.data["text"] = first_text[m.end():]


def _handle_api_result(result: Optional[Dict[str, Any]]) -> Any:
    if isinstance(result, dict):
        if result.get("status") == "failed":
            raise ActionFailed(retcode=result.get("retcode"))
        return result.get("data")


class ResultStore:
    _seq = 1
    _futures: Dict[int, asyncio.Future] = {}

    @classmethod
    def get_seq(cls) -> int:
        s = cls._seq
        cls._seq = (cls._seq + 1) % sys.maxsize
        return s

    @classmethod
    def add_result(cls, result: Dict[str, Any]):
        if isinstance(result.get("echo"), dict) and \
                isinstance(result["echo"].get("seq"), int):
            future = cls._futures.get(result["echo"]["seq"])
            if future:
                future.set_result(result)

    @classmethod
    async def fetch(cls, seq: int, timeout: Optional[float]) -> Dict[str, Any]:
        future = asyncio.get_event_loop().create_future()
        cls._futures[seq] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise NetworkError("WebSocket API call timeout")
        finally:
            del cls._futures[seq]


class Bot(BaseBot):

    def __init__(self,
                 driver: Driver,
                 connection_type: str,
                 config: Config,
                 self_id: str,
                 *,
                 websocket: WebSocket = None):
        if connection_type not in ["http", "websocket"]:
            raise ValueError("Unsupported connection type")

        super().__init__(driver,
                         connection_type,
                         config,
                         self_id,
                         websocket=websocket)

    @property
    @overrides(BaseBot)
    def type(self) -> str:
        return "cqhttp"

    @overrides(BaseBot)
    async def handle_message(self, message: dict):
        if not message:
            return

        if "post_type" not in message:
            ResultStore.add_result(message)
            return

        event = Event(message)

        # Check whether user is calling me
        # TODO: Check reply
        _check_at_me(self, event)
        _check_nickname(self, event)

        await handle_event(self, event)

    @overrides(BaseBot)
    async def call_api(self, api: str, **data) -> Union[Any, NoReturn]:
        if "self_id" in data:
            self_id = data.pop("self_id")
            if self_id:
                bot = self.driver.bots[str(self_id)]
                return await bot.call_api(api, **data)

        if self.type == "websocket":
            seq = ResultStore.get_seq()
            await self.websocket.send({
                "action": api,
                "params": data,
                "echo": {
                    "seq": seq
                }
            })
            return _handle_api_result(await ResultStore.fetch(
                seq, self.config.api_timeout))

        elif self.type == "http":
            api_root = self.config.api_root.get(self.self_id)
            if not api_root:
                raise ApiNotAvailable
            elif not api_root.endswith("/"):
                api_root += "/"

            headers = {}
            if self.config.access_token is not None:
                headers["Authorization"] = "Bearer " + self.config.access_token

            try:
                async with httpx.AsyncClient(headers=headers) as client:
                    response = await client.post(
                        api_root + api,
                        json=data,
                        timeout=self.config.api_timeout)

                if 200 <= response.status_code < 300:
                    result = response.json()
                    return _handle_api_result(result)
                raise NetworkError(f"HTTP request received unexpected "
                                   f"status code: {response.status_code}")
            except httpx.InvalidURL:
                raise NetworkError("API root url invalid")
            except httpx.HTTPError:
                raise NetworkError("HTTP request failed")

    @overrides(BaseBot)
    async def send(self, event: "Event", message: Union[str, "Message",
                                                        "MessageSegment"],
                   **kwargs) -> Union[Any, NoReturn]:
        msg = message if isinstance(message, Message) else Message(message)

        at_sender = kwargs.pop("at_sender", False) and bool(event.user_id)

        params = {}
        if event.user_id:
            params["user_id"] = event.user_id
        if event.group_id:
            params["group_id"] = event.group_id
        params.update(kwargs)

        if "message_type" not in params:
            if "group_id" in params:
                params["message_type"] = "group"
            elif "user_id" in params:
                params["message_type"] = "private"
            else:
                raise ValueError("Cannot guess message type to reply!")

        if at_sender and params["message_type"] != "private":
            params["message"] = MessageSegment.at(params["user_id"]) + \
                MessageSegment.text(" ") + msg
        else:
            params["message"] = msg
        return await self.send_msg(**params)


class Event(BaseEvent):

    def __init__(self, raw_event: dict):
        if "message" in raw_event:
            raw_event["message"] = Message(raw_event["message"])

        super().__init__(raw_event)

    @property
    @overrides(BaseEvent)
    def id(self) -> Optional[int]:
        return self._raw_event.get("message_id") or self._raw_event.get("flag")

    @property
    @overrides(BaseEvent)
    def name(self) -> str:
        n = self.type + "." + self.detail_type
        if self.sub_type:
            n += "." + self.sub_type
        return n

    @property
    @overrides(BaseEvent)
    def self_id(self) -> str:
        return str(self._raw_event["self_id"])

    @property
    @overrides(BaseEvent)
    def time(self) -> int:
        return self._raw_event["time"]

    @property
    @overrides(BaseEvent)
    def type(self) -> str:
        return self._raw_event["post_type"]

    @type.setter
    @overrides(BaseEvent)
    def type(self, value) -> None:
        self._raw_event["post_type"] = value

    @property
    @overrides(BaseEvent)
    def detail_type(self) -> str:
        return self._raw_event[f"{self.type}_type"]

    @detail_type.setter
    @overrides(BaseEvent)
    def detail_type(self, value) -> None:
        self._raw_event[f"{self.type}_type"] = value

    @property
    @overrides(BaseEvent)
    def sub_type(self) -> Optional[str]:
        return self._raw_event.get("sub_type")

    @type.setter
    @overrides(BaseEvent)
    def sub_type(self, value) -> None:
        self._raw_event["sub_type"] = value

    @property
    @overrides(BaseEvent)
    def user_id(self) -> Optional[int]:
        return self._raw_event.get("user_id")

    @user_id.setter
    @overrides(BaseEvent)
    def user_id(self, value) -> None:
        self._raw_event["user_id"] = value

    @property
    @overrides(BaseEvent)
    def group_id(self) -> Optional[int]:
        return self._raw_event.get("group_id")

    @group_id.setter
    @overrides(BaseEvent)
    def group_id(self, value) -> None:
        self._raw_event["group_id"] = value

    @property
    @overrides(BaseEvent)
    def to_me(self) -> Optional[bool]:
        return self._raw_event.get("to_me")

    @to_me.setter
    @overrides(BaseEvent)
    def to_me(self, value) -> None:
        self._raw_event["to_me"] = value

    @property
    @overrides(BaseEvent)
    def message(self) -> Optional["Message"]:
        return self._raw_event.get("message")

    @message.setter
    @overrides(BaseEvent)
    def message(self, value) -> None:
        self._raw_event["message"] = value

    @property
    @overrides(BaseEvent)
    def raw_message(self) -> Optional[str]:
        return self._raw_event.get("raw_message")

    @raw_message.setter
    @overrides(BaseEvent)
    def raw_message(self, value) -> None:
        self._raw_event["raw_message"] = value

    @property
    @overrides(BaseEvent)
    def plain_text(self) -> Optional[str]:
        return self.message and self.message.extract_plain_text()

    @property
    @overrides(BaseEvent)
    def sender(self) -> Optional[dict]:
        return self._raw_event.get("sender")

    @sender.setter
    @overrides(BaseEvent)
    def sender(self, value) -> None:
        self._raw_event["sender"] = value


class MessageSegment(BaseMessageSegment):

    @overrides(BaseMessageSegment)
    def __init__(self, type: str, data: Dict[str, Union[str, list]]) -> None:
        if type == "text":
            data["text"] = unescape(data["text"])
        super().__init__(type=type, data=data)

    @overrides(BaseMessageSegment)
    def __str__(self):
        type_ = self.type
        data = self.data.copy()

        # process special types
        if type_ == "text":
            return escape(data.get("text", ""), escape_comma=False)

        params = ",".join(
            [f"{k}={escape(str(v))}" for k, v in data.items() if v is not None])
        return f"[CQ:{type_}{',' if params else ''}{params}]"

    @overrides(BaseMessageSegment)
    def __add__(self, other) -> "Message":
        return Message(self) + other

    @staticmethod
    def anonymous(ignore_failure: Optional[bool] = None) -> "MessageSegment":
        return MessageSegment("anonymous", {"ignore": _b2s(ignore_failure)})

    @staticmethod
    def at(user_id: Union[int, str]) -> "MessageSegment":
        return MessageSegment("at", {"qq": str(user_id)})

    @staticmethod
    def contact_group(group_id: int) -> "MessageSegment":
        return MessageSegment("contact", {"type": "group", "id": str(group_id)})

    @staticmethod
    def contact_user(user_id: int) -> "MessageSegment":
        return MessageSegment("contact", {"type": "qq", "id": str(user_id)})

    @staticmethod
    def dice() -> "MessageSegment":
        return MessageSegment("dice", {})

    @staticmethod
    def face(id_: int) -> "MessageSegment":
        return MessageSegment("face", {"id": str(id_)})

    @staticmethod
    def forward(id_: str) -> "MessageSegment":
        logger.warning("Forward Message only can be received!")
        return MessageSegment("forward", {"id": id_})

    @staticmethod
    def image(file: str,
              type_: Optional[str] = None,
              cache: bool = True,
              proxy: bool = True,
              timeout: Optional[int] = None) -> "MessageSegment":
        return MessageSegment(
            "image", {
                "file": file,
                "type": type_,
                "cache": cache,
                "proxy": proxy,
                "timeout": timeout
            })

    @staticmethod
    def json(data: str) -> "MessageSegment":
        return MessageSegment("json", {"data": data})

    @staticmethod
    def location(latitude: float,
                 longitude: float,
                 title: Optional[str] = None,
                 content: Optional[str] = None) -> "MessageSegment":
        return MessageSegment(
            "location", {
                "lat": str(latitude),
                "lon": str(longitude),
                "title": title,
                "content": content
            })

    @staticmethod
    def music(type_: str, id_: int) -> "MessageSegment":
        return MessageSegment("music", {"type": type_, "id": id_})

    @staticmethod
    def music_custom(url: str,
                     audio: str,
                     title: str,
                     content: Optional[str] = None,
                     img_url: Optional[str] = None) -> "MessageSegment":
        return MessageSegment(
            "music", {
                "type": "custom",
                "url": url,
                "audio": audio,
                "title": title,
                "content": content,
                "image": img_url
            })

    @staticmethod
    def node(id_: int) -> "MessageSegment":
        return MessageSegment("node", {"id": str(id_)})

    @staticmethod
    def node_custom(user_id: int, nickname: str,
                    content: Union[str, "Message"]) -> "MessageSegment":
        return MessageSegment("node", {
            "user_id": str(user_id),
            "nickname": nickname,
            "content": content
        })

    @staticmethod
    def poke(type_: str, id_: str) -> "MessageSegment":
        return MessageSegment("poke", {"type": type_, "id": id_})

    @staticmethod
    def record(file: str,
               magic: Optional[bool] = None,
               cache: Optional[bool] = None,
               proxy: Optional[bool] = None,
               timeout: Optional[int] = None) -> "MessageSegment":
        return MessageSegment("record", {"file": file, "magic": _b2s(magic)})

    @staticmethod
    def reply(id_: int) -> "MessageSegment":
        return MessageSegment("reply", {"id": str(id_)})

    @staticmethod
    def rps() -> "MessageSegment":
        return MessageSegment("rps", {})

    @staticmethod
    def shake() -> "MessageSegment":
        return MessageSegment("shake", {})

    @staticmethod
    def share(url: str = "",
              title: str = "",
              content: Optional[str] = None,
              img_url: Optional[str] = None) -> "MessageSegment":
        return MessageSegment("share", {
            "url": url,
            "title": title,
            "content": content,
            "img_url": img_url
        })

    @staticmethod
    def text(text: str) -> "MessageSegment":
        return MessageSegment("text", {"text": text})

    @staticmethod
    def video(file: str,
              cache: Optional[bool] = None,
              proxy: Optional[bool] = None,
              timeout: Optional[int] = None) -> "MessageSegment":
        return MessageSegment("video", {
            "file": file,
            "cache": cache,
            "proxy": proxy,
            "timeout": timeout
        })

    @staticmethod
    def xml(data: str) -> "MessageSegment":
        return MessageSegment("xml", {"data": data})


class Message(BaseMessage):

    @staticmethod
    @overrides(BaseMessage)
    def _construct(msg: Union[str, dict, list]) -> Iterable[MessageSegment]:
        if isinstance(msg, dict):
            yield MessageSegment(msg["type"], msg.get("data") or {})
            return
        elif isinstance(msg, list):
            for seg in msg:
                yield MessageSegment(seg["type"], seg.get("data") or {})
            return

        def _iter_message(msg: str) -> Iterable[Tuple[str, str]]:
            text_begin = 0
            for cqcode in re.finditer(
                    r"\[CQ:(?P<type>[a-zA-Z0-9-_.]+)"
                    r"(?P<params>"
                    r"(?:,[a-zA-Z0-9-_.]+=?[^,\]]*)*"
                    r"),?\]", msg):
                yield "text", unescape(msg[text_begin:cqcode.pos +
                                           cqcode.start()])
                text_begin = cqcode.pos + cqcode.end()
                yield cqcode.group("type"), cqcode.group("params").lstrip(",")
            yield "text", unescape(msg[text_begin:])

        for type_, data in _iter_message(msg):
            if type_ == "text":
                if data:
                    # only yield non-empty text segment
                    yield MessageSegment(type_, {"text": data})
            else:
                data = {
                    k: v for k, v in map(
                        lambda x: x.split("=", maxsplit=1),
                        filter(lambda x: x, (
                            x.lstrip() for x in data.split(","))))
                }
                yield MessageSegment(type_, data)
