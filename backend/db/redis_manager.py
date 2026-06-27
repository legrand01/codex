"""
Redis connection management using redis.asyncio.

Supports regular Redis commands, pub/sub, and Redis Streams
for real-time event distribution across the platform.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

import redis.asyncio as aioredis

from backend.config import settings

logger = logging.getLogger(__name__)

# Module-level Redis client references
_redis_client: Optional[aioredis.Redis] = None
_pubsub: Optional[aioredis.client.PubSub] = None


async def create_redis_client() -> aioredis.Redis:
    """
    Create and return a Redis async client.

    Uses settings from backend.config for the Redis URL.
    Stores the client in the module-level variable for later retrieval.

    Returns:
        The created aioredis.Redis instance.
    """
    global _redis_client
    if _redis_client is not None:
        logger.warning("Redis client already exists. Returning existing client.")
        return _redis_client

    logger.info("Creating Redis async client...")
    _redis_client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    logger.info(f"Redis client created (url={settings.redis_url}).")
    return _redis_client


def get_redis_client() -> Optional[aioredis.Redis]:
    """
    Get the current Redis client.

    Returns:
        The aioredis.Redis instance, or None if not yet created.
    """
    return _redis_client


async def close_redis_client() -> None:
    """
    Close the Redis client and release connections.

    Also closes any active pub/sub subscriptions.
    Safe to call even if the client is not initialized.
    """
    global _redis_client, _pubsub
    if _pubsub is not None:
        logger.info("Closing Redis pub/sub...")
        await _pubsub.close()
        _pubsub = None

    if _redis_client is not None:
        logger.info("Closing Redis client...")
        await _redis_client.close()
        _redis_client = None
        logger.info("Redis client closed.")
    else:
        logger.debug("No Redis client to close.")


async def get_pubsub() -> aioredis.client.PubSub:
    """
    Get or create a pub/sub instance from the Redis client.

    Returns:
        An aioredis PubSub instance.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    global _pubsub
    if _redis_client is None:
        raise RuntimeError(
            "Redis client is not initialized. "
            "Ensure the application lifespan has started."
        )
    if _pubsub is None:
        _pubsub = _redis_client.pubsub()
    return _pubsub


async def publish(channel: str, message: str) -> int:
    """
    Publish a message to a Redis pub/sub channel.

    Args:
        channel: The channel name to publish to.
        message: The message payload (string).

    Returns:
        The number of subscribers that received the message.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized.")
    return await _redis_client.publish(channel, message)


async def subscribe(channel: str, handler: Optional[Callable] = None) -> None:
    """
    Subscribe to a Redis pub/sub channel.

    Args:
        channel: The channel name to subscribe to.
        handler: Optional callback function for messages on this channel.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    pubsub = await get_pubsub()
    if handler:
        await pubsub.subscribe(**{channel: handler})
    else:
        await pubsub.subscribe(channel)
    logger.info(f"Subscribed to channel: {channel}")


async def unsubscribe(channel: str) -> None:
    """
    Unsubscribe from a Redis pub/sub channel.

    Args:
        channel: The channel name to unsubscribe from.
    """
    pubsub = await get_pubsub()
    await pubsub.unsubscribe(channel)
    logger.info(f"Unsubscribed from channel: {channel}")


# --- Redis Streams support ---


async def stream_add(
    stream: str,
    fields: Dict[str, str],
    max_len: Optional[int] = None,
) -> str:
    """
    Add an entry to a Redis Stream.

    Args:
        stream: The stream key name.
        fields: Dictionary of field-value pairs for the stream entry.
        max_len: Optional maximum stream length (approximate trimming).

    Returns:
        The auto-generated entry ID.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized.")

    kwargs: Dict[str, Any] = {}
    if max_len is not None:
        kwargs["maxlen"] = max_len
        kwargs["approximate"] = True

    entry_id = await _redis_client.xadd(stream, fields, **kwargs)
    return entry_id


async def stream_read(
    streams: Dict[str, str],
    count: Optional[int] = None,
    block: Optional[int] = None,
) -> List:
    """
    Read entries from one or more Redis Streams.

    Args:
        streams: Dictionary mapping stream names to last-read entry IDs
                 (use '0' for all entries, '$' for new entries only).
        count: Maximum number of entries to return per stream.
        block: Block for this many milliseconds waiting for new entries
               (0 = block indefinitely, None = don't block).

    Returns:
        List of stream entries in the redis-py format.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized.")

    kwargs: Dict[str, Any] = {}
    if count is not None:
        kwargs["count"] = count
    if block is not None:
        kwargs["block"] = block

    return await _redis_client.xread(streams, **kwargs)


async def stream_create_consumer_group(
    stream: str,
    group: str,
    start_id: str = "0",
    mkstream: bool = True,
) -> bool:
    """
    Create a consumer group for a Redis Stream.

    Args:
        stream: The stream key name.
        group: The consumer group name.
        start_id: The starting entry ID for the group ('0' for all, '$' for new only).
        mkstream: Create the stream if it does not exist.

    Returns:
        True if the group was created successfully.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized.")

    try:
        await _redis_client.xgroup_create(
            stream, group, id=start_id, mkstream=mkstream
        )
        logger.info(f"Consumer group '{group}' created for stream '{stream}'.")
        return True
    except aioredis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logger.debug(f"Consumer group '{group}' already exists for stream '{stream}'.")
            return True
        raise


async def stream_read_group(
    group: str,
    consumer: str,
    streams: Dict[str, str],
    count: Optional[int] = None,
    block: Optional[int] = None,
) -> List:
    """
    Read entries from a Redis Stream as part of a consumer group.

    Args:
        group: The consumer group name.
        consumer: The consumer name within the group.
        streams: Dictionary mapping stream names to entry IDs
                 (use '>' for new undelivered entries).
        count: Maximum number of entries to return per stream.
        block: Block for this many milliseconds waiting for new entries.

    Returns:
        List of stream entries in the redis-py format.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized.")

    kwargs: Dict[str, Any] = {}
    if count is not None:
        kwargs["count"] = count
    if block is not None:
        kwargs["block"] = block

    return await _redis_client.xreadgroup(group, consumer, streams, **kwargs)


async def stream_ack(stream: str, group: str, *entry_ids: str) -> int:
    """
    Acknowledge one or more entries in a consumer group.

    Args:
        stream: The stream key name.
        group: The consumer group name.
        *entry_ids: One or more entry IDs to acknowledge.

    Returns:
        The number of entries successfully acknowledged.

    Raises:
        RuntimeError: If the Redis client has not been initialized.
    """
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized.")

    return await _redis_client.xack(stream, group, *entry_ids)
