import io

from .base import Storage, StorageError


class S3Storage(Storage):
    """
    Stores backups in an S3-compatible bucket (AWS S3, MinIO, Ceph RGW, Wasabi, Backblaze B2, ...).
    """
    name = 's3'

    def __init__(self, bucket, prefix='', endpoint_url=None, region_name=None, access_key_id=None,
                 secret_access_key=None, session_token=None, verify=True, storage_class=None,
                 server_side_encryption=None, sse_kms_key_id=None, addressing_style=None, **config):
        super().__init__(bucket=bucket, prefix=prefix, **config)
        try:
            import boto3
            from boto3.s3.transfer import TransferConfig
            from botocore.config import Config
        except ImportError as e:
            raise StorageError(
                "The S3 storage backend requires boto3: pip install 'netbox-backup[s3]'"
            ) from e
        self.bucket = bucket
        self.prefix = (prefix or '').strip('/')
        self.endpoint_url = endpoint_url
        self.region_name = region_name
        self.storage_class = storage_class
        self.server_side_encryption = server_side_encryption
        self.sse_kms_key_id = sse_kms_key_id
        client_config = Config(
            retries={'max_attempts': 10, 'mode': 'adaptive'},
            s3={'addressing_style': addressing_style} if addressing_style else None,
        )
        self.client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            aws_session_token=session_token,
            verify=verify,
            config=client_config,
        )
        chunk = 64 * 1024 * 1024
        self.transfer_config = TransferConfig(multipart_threshold=chunk, multipart_chunksize=chunk)

    def describe(self):
        return {
            'backend': 's3',
            'bucket': self.bucket,
            'prefix': self.prefix or '/',
            'endpoint_url': self.endpoint_url or 'AWS default',
            'region_name': self.region_name or 'default',
            'server_side_encryption': self.server_side_encryption or 'none',
        }

    def _key(self, key):
        return self.join(self.prefix, key)

    def _extra_args(self):
        args = {}
        if self.storage_class:
            args['StorageClass'] = self.storage_class
        if self.server_side_encryption:
            args['ServerSideEncryption'] = self.server_side_encryption
        if self.sse_kms_key_id:
            args['SSEKMSKeyId'] = self.sse_kms_key_id
        return args

    def _wrap(self, func, *args, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return func(*args, **kwargs)
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code')
            if code in ('404', 'NoSuchKey', 'NotFound'):
                raise StorageError(f"Object not found: {args[0] if args else ''}") from e
            raise StorageError(f"S3 error ({code}): {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"S3 error: {e}") from e

    def put_file(self, key, local_path):
        self._wrap(
            self.client.upload_file, local_path, self.bucket, self._key(key),
            ExtraArgs=self._extra_args() or None, Config=self.transfer_config,
        )

    def get_file(self, key, local_path):
        self._wrap(self.client.download_file, self.bucket, self._key(key), local_path, Config=self.transfer_config)

    def put_bytes(self, key, data):
        self._wrap(self.client.put_object, Bucket=self.bucket, Key=self._key(key), Body=data, **self._extra_args())

    def get_bytes(self, key):
        response = self._wrap(self.client.get_object, Bucket=self.bucket, Key=self._key(key))
        return response['Body'].read()

    def exists(self, key):
        try:
            self._wrap(self.client.head_object, Bucket=self.bucket, Key=self._key(key))
            return True
        except StorageError as e:
            if 'not found' in str(e).lower():
                return False
            raise

    def size(self, key):
        response = self._wrap(self.client.head_object, Bucket=self.bucket, Key=self._key(key))
        return response['ContentLength']

    def list_children(self, prefix=''):
        full = self._key(prefix)
        full = f'{full}/' if full else ''
        paginator = self.client.get_paginator('list_objects_v2')
        children = set()
        pages = self._wrap(lambda: list(paginator.paginate(Bucket=self.bucket, Prefix=full, Delimiter='/')))
        for page in pages:
            for cp in page.get('CommonPrefixes', []):
                name = cp['Prefix'][len(full):].strip('/')
                if name and not name.startswith('.'):
                    children.add(name)
        return sorted(children)

    def delete_prefix(self, prefix):
        if not prefix.strip('/'):
            raise StorageError("Refusing to delete the storage root")
        full = self._key(prefix)
        paginator = self.client.get_paginator('list_objects_v2')
        keys = []
        # Match the exact key and anything beneath it as a "directory"
        for candidate in (full, f'{full}/'):
            pages = self._wrap(lambda c=candidate: list(paginator.paginate(Bucket=self.bucket, Prefix=c)))
            for page in pages:
                for obj in page.get('Contents', []):
                    if obj['Key'] == full or obj['Key'].startswith(f'{full}/'):
                        keys.append(obj['Key'])
        keys = sorted(set(keys))
        for i in range(0, len(keys), 1000):
            batch = [{'Key': k} for k in keys[i:i + 1000]]
            response = self._wrap(self.client.delete_objects, Bucket=self.bucket, Delete={'Objects': batch})
            if errors := response.get('Errors'):
                raise StorageError(f"Failed to delete {len(errors)} object(s), first: {errors[0]}")

    def open_read(self, key):
        response = self._wrap(self.client.get_object, Bucket=self.bucket, Key=self._key(key))
        return _StreamingReader(response['Body'])


class _StreamingReader(io.RawIOBase):
    def __init__(self, body):
        self.body = body

    def readable(self):
        return True

    def read(self, size=-1):
        return self.body.read(None if size is None or size < 0 else size)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)

    def close(self):
        self.body.close()
        super().close()
