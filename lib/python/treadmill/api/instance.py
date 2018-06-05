"""Implementation of instance API.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import fnmatch
import logging

from treadmill import admin
from treadmill import context
from treadmill import exc
from treadmill import schema
from treadmill import utils
from treadmill import plugin_manager
from treadmill.scheduler import masterapi
from treadmill.api import app


_LOGGER = logging.getLogger(__name__)


# TODO: these are temporary hardcoded quotas on total number of scheduled
#       apps and total allowed apps per-proid.
#       The goal is to manage these eventually. Immediate goal is to prevent
#       DoS attack on the scheduler.

_TOTAL_SCHEDULED_QUOTA = 50000

_PROID_SCHEDULED_QUOTA = 10000


@schema.schema(
    {'allOf': [{'$ref': 'instance.json#/resource'},
               {'$ref': 'instance.json#/verbs/schedule'}]},
)
def _validate(rsrc):
    """Validate instance manifest."""
    memory_mb = utils.megabytes(rsrc['memory'])
    if memory_mb < 100:
        raise exc.InvalidInputError(
            __name__,
            'memory size should be larger than or equal to 100M')

    disk_mb = utils.megabytes(rsrc['disk'])
    if disk_mb < 100:
        raise exc.InvalidInputError(
            __name__,
            'disk size should be larger than or equal to 100M')


def _check_required_attributes(configured):
    """Check that all required attributes are populated."""
    if 'proid' not in configured:
        raise exc.TreadmillError(
            'Missing required attribute: proid')

    if 'environment' not in configured:
        raise exc.TreadmillError(
            'Missing required attribute: environment')


def _set_defaults(configured, rsrc_id):
    """Set defaults."""
    if 'identity_group' not in configured:
        configured['identity_group'] = None

    # TODO: default affinity can be and probably better set in instance plugin.
    #       if it is not set, seems reasonable to just set it to app name and
    #       not introduce special meaning for the instance id components.
    if 'affinity' not in configured:
        configured['affinity'] = '{0}.{1}'.format(*rsrc_id.split('.'))


def _api_plugins(plugins):
    """Return instance plugins."""
    if not plugins:
        return []

    plugins_ns = 'treadmill.api.instance.plugins'
    _LOGGER.info('Instance api plugins: %r', plugins)
    return [
        plugin_manager.load(plugins_ns, name)
        for name in plugins
    ]


class API(object):
    """Treadmill Instance REST api."""

    def __init__(self, plugins=None):

        # Since the API attributes are wrapped (journal, authz, etc) - all non
        # methods should be protected.
        self._plugins = _api_plugins(plugins)

        def _list(match=None):
            """List configured instances."""
            if match is None:
                match = '*'
            if '#' not in match:
                match += '#*'

            instances = masterapi.list_scheduled_apps(context.GLOBAL.zk.conn)
            filtered = [
                inst for inst in instances
                if fnmatch.fnmatch(inst, match)
            ]
            return sorted(filtered)

        @schema.schema({'$ref': 'instance.json#/resource_id'})
        def get(rsrc_id):
            """Get instance configuration."""
            inst = masterapi.get_app(context.GLOBAL.zk.conn, rsrc_id)
            if inst is None:
                return inst

            inst['_id'] = rsrc_id
            for plugin in self._plugins:
                inst = plugin.remove_attributes(inst)
            return inst

        @schema.schema(
            {'$ref': 'app.json#/resource_id'},
            {'allOf': [{'$ref': 'instance.json#/resource'},
                       {'$ref': 'instance.json#/verbs/create'}]},
            count={'type': 'integer', 'minimum': 1, 'maximum': 1000},
            created_by={'anyOf': [
                {'type': 'null'},
                {'$ref': 'common.json#/user'},
            ]}
        )
        def create(rsrc_id, rsrc, count=1, created_by=None):
            """Create (configure) instance."""
            _LOGGER.info('create: count = %s, %s %r, created_by = %s',
                         count, rsrc_id, rsrc, created_by)

            # Check scheduled quota.
            zkclient = context.GLOBAL.zk.conn
            scheduled_stats = masterapi.get_scheduled_stats(zkclient)

            total_apps = sum(scheduled_stats.values())
            if total_apps + count > _TOTAL_SCHEDULED_QUOTA:
                raise exc.QuotaExceededError(
                    'Total scheduled apps quota exceeded.')

            proid_apps = scheduled_stats.get(rsrc_id[:rsrc_id.find('.')], 0)
            if proid_apps + count > _PROID_SCHEDULED_QUOTA:
                raise exc.QuotaExceededError(
                    'Proid scheduled apps quota exceeded.')

            admin_app = admin.Application(context.GLOBAL.ldap.conn)
            if not rsrc:
                configured = admin_app.get(rsrc_id)
            else:
                # Make sure defaults are present
                configured = admin_app.from_entry(admin_app.to_entry(rsrc))
                app.verify_feature(rsrc.get('features', []))

            if 'services' in configured and not configured['services']:
                del configured['services']
            if '_id' in configured:
                del configured['_id']

            _LOGGER.info('Configured: %s %r', rsrc_id, configured)

            _validate(configured)

            for plugin in self._plugins:
                configured = plugin.add_attributes(rsrc_id, configured)

            _check_required_attributes(configured)
            _set_defaults(configured, rsrc_id)

            scheduled = masterapi.create_apps(
                zkclient, rsrc_id, configured, count, created_by
            )
            return scheduled

        @schema.schema(
            {'$ref': 'instance.json#/resource_id'},
            {'allOf': [{'$ref': 'instance.json#/verbs/update'}]}
        )
        def update(rsrc_id, rsrc):
            """Update instance configuration."""
            _LOGGER.info('update: %s %r', rsrc_id, rsrc)

            delta = {rsrc_id: rsrc['priority']}

            masterapi.update_app_priorities(context.GLOBAL.zk.conn, delta)
            return masterapi.get_app(context.GLOBAL.zk.conn, rsrc_id)

        @schema.schema(
            {'type': 'array',
             'items': {'$ref': 'instance.json#/verbs/update'},
             'minItems': 1}
        )
        def bulk_update(updates):
            """Bulk update instance priorities."""
            _LOGGER.info('update: %r', updates)

            def _process(rsrc):
                try:
                    if '_id' not in rsrc:
                        raise exc.InvalidInputError(
                            __name__,
                            'delta is missing _id attribute: {}'.format(rsrc)
                        )
                    rsrc_id = rsrc['_id']
                    delta = {rsrc_id: rsrc['priority']}
                    masterapi.update_app_priorities(
                        context.GLOBAL.zk.conn,
                        delta
                    )
                    return masterapi.get_app(context.GLOBAL.zk.conn, rsrc_id)
                except Exception as err:  # pylint: disable=W0703
                    return {'_error': {'_id': rsrc_id,
                                       'why': str(err)}}

            return [_process(rsrc) for rsrc in updates]

        @schema.schema(
            {'$ref': 'instance.json#/resource_id'},
            deleted_by={'anyOf': [
                {'type': 'null'},
                {'$ref': 'common.json#/user'},
            ]}
        )
        def delete(rsrc_id, deleted_by=None):
            """Delete configured instance."""
            _LOGGER.info('delete: %s, deleted_by = %s', rsrc_id, deleted_by)

            masterapi.delete_apps(
                context.GLOBAL.zk.conn, [rsrc_id], deleted_by
            )

        @schema.schema(
            {'$ref': 'instance.json#/resource_ids'},
            deleted_by={'anyOf': [
                {'type': 'null'},
                {'$ref': 'common.json#/user'},
            ]}
        )
        def bulk_delete(rsrc_ids, deleted_by=None):
            """Bulk delete with resource instance IDs
            """
            _LOGGER.info('delete: %r, deleted_by = %s', rsrc_ids, deleted_by)

            masterapi.delete_apps(
                context.GLOBAL.zk.conn, rsrc_ids, deleted_by
            )

        self.list = _list
        self.get = get
        self.create = create
        self.update = update
        self.delete = delete
        self.bulk_update = bulk_update
        self.bulk_delete = bulk_delete
