from datetime import datetime, timedelta
import json

from celery.schedules import schedule, crontab
from celery.utils.timeutils import maybe_timedelta
from celery.tests.case import AppCase

from fakeredis import FakeStrictRedis

from redbeat import RedBeatScheduler, RedBeatSchedulerEntry
from redbeat.schedulers import add_defaults
from redbeat.decoder import RedBeatJSONDecoder, RedBeatJSONEncoder


class RedBeatCase(AppCase):

    def setup(self):
        self.app.conf.add_defaults({
            'REDBEAT_KEY_PREFIX': 'rb-tests:',
        })
        add_defaults(self.app)

        self.app.redbeat_redis = FakeStrictRedis()
        self.app.redbeat_redis.flushdb()

    def create_entry(self, name=None, task=None, s=None, **kwargs):

        if name is None:
            name = 'test'

        if task is None:
            task = 'tasks.test'

        if s is None:
            s = schedule(run_every=60)

        e = RedBeatSchedulerEntry(name, task, s, app=self.app, **kwargs)

        return e


class test_RedBeatEntry(RedBeatCase):

    def test_basic_save(self):
        e = self.create_entry()
        e.save()

        expected = {
            'name': 'test',
            'task': 'tasks.test',
            'schedule': e.schedule,
            'args': None,
            'kwargs': None,
            'options': {},
            'enabled': True,
        }

        redis = self.app.redbeat_redis
        value = redis.hget(self.app.conf.REDBEAT_KEY_PREFIX + 'test', 'definition')
        self.assertEqual(expected, json.loads(value, cls=RedBeatJSONDecoder))
        self.assertEqual(redis.zrank(self.app.conf.REDBEAT_SCHEDULE_KEY, e.key), 0)
        self.assertEqual(redis.zscore(self.app.conf.REDBEAT_SCHEDULE_KEY, e.key), e.score)

    def test_load_meta_nonexistent_key(self):
        meta = RedBeatSchedulerEntry.load_meta('doesntexist', self.app)
        self.assertEqual(meta, {'last_run_at': datetime.min})

    def test_load_definition_nonexistent_key(self):
        with self.assertRaises(KeyError):
            RedBeatSchedulerEntry.load_definition('doesntexist', self.app)

    def test_from_key_nonexistent_key(self):
        with self.assertRaises(KeyError):
            RedBeatSchedulerEntry.from_key('doesntexist', self.app)

    def test_next(self):
        initial = self.create_entry()
        now = self.app.now()

        n = initial.next(last_run_at=now)

        # new entry has updated run info
        self.assertNotEqual(initial, n)
        self.assertEqual(n.last_run_at, now)
        self.assertEqual(initial.total_run_count + 1, n.total_run_count)

        # updated meta was stored into redis
        meta = RedBeatSchedulerEntry.load_meta(initial.key, app=self.app)
        self.assertEqual(meta['last_run_at'], now)
        self.assertEqual(meta['total_run_count'], initial.total_run_count + 1)

        # new entry updated the schedule
        redis = self.app.redbeat_redis
        self.assertEqual(redis.zscore(self.app.conf.REDBEAT_SCHEDULE_KEY, n.key), n.score)

    def test_next_only_update_last_run_at(self):
        initial = self.create_entry()

        n = initial.next(only_update_last_run_at=True)
        self.assertGreater(n.last_run_at, initial.last_run_at)
        self.assertEqual(n.total_run_count, initial.total_run_count)

    def test_delete(self):
        initial = self.create_entry()
        initial.save()

        e = RedBeatSchedulerEntry.from_key(initial.key, app=self.app)
        e.delete()

        exists = self.app.redbeat_redis.exists(initial.key)
        self.assertFalse(exists)

        score = self.app.redbeat_redis.zrank(self.app.conf.REDBEAT_SCHEDULE_KEY, initial.key)
        self.assertIsNone(score)


class mocked_schedule(schedule):

    def __init__(self, remaining):
        self._remaining = maybe_timedelta(remaining)
        self.run_every = timedelta(seconds=1)
        self.nowfun = datetime.utcnow

    def remaining_estimate(self, last_run_at):
        return self._remaining


due_now = mocked_schedule(0)


class test_RedBeatScheduler(RedBeatCase):

    def create_scheduler(self):
        return RedBeatScheduler(app=self.app)

    def test_empty_schedule(self):
        s = self.create_scheduler()

        self.assertEqual(s.schedule, {})
        sleep = s.tick()
        self.assertEqual(sleep, s.max_interval)

    def test_schedule_includes_current_and_next(self):
        s = self.create_scheduler()

        due = self.create_entry(name='due', s=due_now).save()
        up_next = self.create_entry(name='up_next', s=mocked_schedule(1)).save()
        way_out = self.create_entry(name='way_out', s=mocked_schedule(s.max_interval * 10)).save()

        schedule = s.schedule
        self.assertEqual(len(schedule), 2)

        self.assertIn(due.name, schedule)
        self.assertEqual(due.key, schedule[due.name].key)

        self.assertIn(up_next.name, schedule)
        self.assertEqual(up_next.key, schedule[up_next.name].key)

        self.assertNotIn(way_out.name, schedule)
