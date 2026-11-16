"""
mongo.py — MongoDB Atlas integration for User Profiles, Job Syncing, and Audit Trails.
"""

import logging
from datetime import datetime
from decouple import config
import pymongo

logger = logging.getLogger(__name__)

_mongo_client = None
_mongo_db = None


def get_mongo_db():
    """
    Returns the MongoDB database instance (singleton connection).
    Returns None if connection fails or MONGO_DB_URI is unset.
    """
    global _mongo_client, _mongo_db
    if _mongo_db is not None:
        return _mongo_db

    uri = config('MONGO_DB_URI', default='')
    if not uri:
        logger.warning("[MongoDB] MONGO_DB_URI not configured in .env")
        return None

    try:
        _mongo_client = pymongo.MongoClient(
            uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
        )
        # Test connection
        _mongo_client.admin.command('ping')
        try:
            _mongo_db = _mongo_client.get_default_database()
        except Exception:
            _mongo_db = _mongo_client['legal_redactor_db']

        logger.info("[MongoDB] Connected successfully to DB: %s", _mongo_db.name)
        return _mongo_db
    except Exception as exc:
        logger.warning("[MongoDB] Connection could not be established: %s", exc)
        return None


def sync_user_profile_to_mongo(user, organization='', role='', bio='', preferred_policy='AUTO'):
    """
    Create or update a user document in MongoDB 'users' collection.
    """
    db = get_mongo_db()
    if db is None:
        return None

    try:
        users_col = db['users']
        now = datetime.utcnow()
        doc = {
            'django_user_id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'organization': organization,
            'role': role,
            'bio': bio,
            'preferred_policy': preferred_policy,
            'is_active': user.is_active,
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'updated_at': now,
        }
        users_col.update_one(
            {'django_user_id': user.id},
            {
                '$set': doc,
                '$setOnInsert': {'created_at': now, 'total_jobs_created': 0}
            },
            upsert=True
        )
        return doc
    except Exception as exc:
        logger.warning("[MongoDB] Failed to sync user profile %s: %s", user.username, exc)
        return None


def get_user_mongo_profile(user_id):
    """
    Fetch a user document from MongoDB by django_user_id.
    """
    db = get_mongo_db()
    if db is None:
        return {}

    try:
        users_col = db['users']
        doc = users_col.find_one({'django_user_id': user_id}, {'_id': 0})
        return doc or {}
    except Exception as exc:
        logger.warning("[MongoDB] Error fetching profile for user %s: %s", user_id, exc)
        return {}


def sync_job_to_mongo(job, extra_metadata=None):
    """
    Sync a RedactionJob and its files into MongoDB 'jobs' collection.
    """
    db = get_mongo_db()
    if db is None:
        return None

    try:
        jobs_col = db['jobs']
        files_data = []
        for f in job.files.all():
            files_data.append({
                'file_id': f.id,
                'filename': f.original_filename,
                'status': f.status,
                'has_redacted': bool(f.redacted_file),
                'has_map': bool(f.redaction_map_file),
            })

        user_info = None
        if hasattr(job, 'user') and job.user:
            user_info = {
                'id': job.user.id,
                'username': job.user.username,
                'email': job.user.email,
            }

        job_doc = {
            'job_id': job.id,
            'user': user_info,
            'status': job.status,
            'created_at': job.created_at,
            'error_message': job.error_message,
            'files_count': job.file_count,
            'files': files_data,
            'metadata': extra_metadata or {},
            'synced_at': datetime.utcnow(),
        }

        jobs_col.update_one(
            {'job_id': job.id},
            {'$set': job_doc},
            upsert=True
        )

        # Update user total_jobs_created counter in Mongo if job has user
        if user_info:
            db['users'].update_one(
                {'django_user_id': job.user.id},
                {'$inc': {'total_jobs_created': 1}}
            )

        return job_doc
    except Exception as exc:
        logger.warning("[MongoDB] Failed to sync job #%s to mongo: %s", job.id, exc)
        return None


def get_user_jobs_from_mongo(user_id, limit=50):
    """
    Fetch the list of jobs created by a user from MongoDB.
    """
    db = get_mongo_db()
    if db is None:
        return []

    try:
        jobs_col = db['jobs']
        cursor = jobs_col.find(
            {'user.id': user_id},
            {'_id': 0}
        ).sort('created_at', pymongo.DESCENDING).limit(limit)
        return list(cursor)
    except Exception as exc:
        logger.warning("[MongoDB] Error querying user jobs from mongo: %s", exc)
        return []
