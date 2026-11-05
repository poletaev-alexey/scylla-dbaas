"""S3 implementation of ``dbaas.scylla.ops.ObjectStore`` (boto3, ``aws`` extra).

Works against AWS S3 and any S3-compatible store (MinIO) via ``endpoint_url``.
"""

import asyncio
from typing import Any


class S3ObjectStore:
    def __init__(self, *, region: str | None = None, endpoint_url: str | None = None) -> None:
        self.region = region
        self.endpoint_url = endpoint_url

    def _client(self) -> Any:
        import boto3  # type: ignore[import-untyped]  # lazy: optional dependency

        return boto3.client("s3", region_name=self.region, endpoint_url=self.endpoint_url)

    def _delete_prefix_sync(self, bucket: str, prefix: str) -> int:
        s3 = self._client()
        deleted = 0
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            # list_objects_v2 pages hold <= 1000 keys, the delete_objects limit.
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
                deleted += len(keys)
        return deleted

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        return await asyncio.to_thread(self._delete_prefix_sync, bucket, prefix)
