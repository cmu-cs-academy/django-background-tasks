# -*- coding: utf-8 -*-
from decimal import Decimal
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.conf import settings
import django
import inspect


from django.utils import timezone
from datetime import timedelta

from hashlib import sha1
import traceback
import logging
from compat import StringIO
from compat import python_2_unicode_compatible
from compat.models import GenericForeignKey
import json

from background_task.signals import task_failed, task_rescheduled


# inspired by http://github.com/tobi/delayed_job
#

# Django 1.6 renamed Manager's get_query_set to get_queryset, and the old
# function will be removed entirely in 1.8. We work back to 1.4, so use a
# metaclass to not worry about it.
# from https://github.com/mysociety/mapit/blob/master/mapit/djangopatch.py#L14-L42

try:
    from django.utils import six
except ImportError:  # Django < 1.4.2
    import six


if django.get_version() < '1.6':
    class GetQuerySetMetaclass(type):
        def __new__(cls, name, bases, attrs):
            new_class = super(GetQuerySetMetaclass, cls).__new__(cls, name, bases, attrs)

            old_method_name = 'get_query_set'
            new_method_name = 'get_queryset'
            for base in inspect.getmro(new_class):
                old_method = base.__dict__.get(old_method_name)
                new_method = base.__dict__.get(new_method_name)

                if not new_method and old_method:
                    setattr(base, new_method_name, old_method)
                if not old_method and new_method:
                    setattr(base, old_method_name, new_method)

            return new_class
elif django.get_version() < '1.8':
    # Nothing to do, make an empty metaclass
    from django.db.models.manager import RenameManagerMethods

    class GetQuerySetMetaclass(RenameManagerMethods):
        pass
else:
    class GetQuerySetMetaclass(type):
        pass


class TaskQuerySet(models.QuerySet):

    def created_by(self, creator):
        """
        :return: A Task queryset filtered by creator
        """
        content_type = ContentType.objects.get_for_model(creator)
        return self.filter(
            creator_content_type=content_type,
            creator_object_id=creator.id,
        )


class TaskManager(six.with_metaclass(GetQuerySetMetaclass, models.Manager)):

    def get_queryset(self):
        return TaskQuerySet(self.model, using=self._db)

    def created_by(self, creator):
        return self.get_queryset().created_by(creator)

    def find_available(self, queue=None):
        now = timezone.now()
        qs = self.unlocked(now)
        if queue:
            qs = qs.filter(queue=queue)
        ready = qs.filter(run_at__lte=now, failed_at=None)
        return ready.order_by('-priority', 'run_at')

    def unlocked(self, now):
        max_run_time = getattr(settings, 'MAX_RUN_TIME', 3600)
        qs = self.get_queryset()
        expires_at = now - timedelta(seconds=max_run_time)
        unlocked = Q(locked_by=None) | Q(locked_at__lt=expires_at)
        return qs.filter(unlocked)

    def new_task(self, task_name, args=None, kwargs=None,
                 run_at=None, priority=0, queue=None, verbose_name=None, creator=None,
                 repeat=None, repeat_until=None):
        args = args or ()
        kwargs = kwargs or {}
        if run_at is None:
            run_at = timezone.now()

        task_params = json.dumps((args, kwargs), sort_keys=True)
        s = "%s%s" % (task_name, task_params)
        task_hash = sha1(s.encode('utf-8')).hexdigest()
        return Task(task_name=task_name,
                    task_params=task_params,
                    task_hash=task_hash,
                    priority=priority,
                    run_at=run_at,
                    queue=queue,
                    verbose_name=verbose_name,
                    creator=creator,
                    repeat=repeat or Task.NEVER,
                    repeat_until=repeat_until,
                    )

    def get_task(self, task_name, args=None, kwargs=None):
        args = args or ()
        kwargs = kwargs or {}
        task_params = json.dumps((args, kwargs), sort_keys=True)
        s = "%s%s" % (task_name, task_params)
        task_hash = sha1(s.encode('utf-8')).hexdigest()
        qs = self.get_queryset()
        return qs.filter(task_hash=task_hash)

    def drop_task(self, task_name, args=None, kwargs=None):
        return self.get_task(task_name, args, kwargs).delete()


@python_2_unicode_compatible
class Task(models.Model):
    # the "name" of the task/function to be run
    task_name = models.CharField(max_length=255, db_index=True)
    # the json encoded parameters to pass to the task
    task_params = models.TextField()
    # a sha1 hash of the name and params, to lookup already scheduled tasks
    task_hash = models.CharField(max_length=40, db_index=True)

    verbose_name = models.CharField(max_length=255, null=True, blank=True)

    # what priority the task has
    priority = models.IntegerField(default=0, db_index=True)
    # when the task should be run
    run_at = models.DateTimeField(db_index=True)

    # Repeat choices are encoded as number of seconds
    # The repeat implementation is based on this encoding
    HOURLY = 3600
    DAILY = 24 * HOURLY
    WEEKLY = 7 * DAILY
    EVERY_2_WEEKS = 2 * WEEKLY
    EVERY_4_WEEKS = 4 * WEEKLY
    NEVER = 0
    REPEAT_CHOICES = (
        (HOURLY, 'hourly'),
        (DAILY, 'daily'),
        (WEEKLY, 'weekly'),
        (EVERY_2_WEEKS, 'every 2 weeks'),
        (EVERY_4_WEEKS, 'every 4 weeks'),
        (NEVER, 'never'),
    )
    repeat = models.BigIntegerField(choices=REPEAT_CHOICES, default=NEVER)
    repeat_until = models.DateTimeField(null=True, blank=True)

    # the "name" of the queue this is to be run on
    queue = models.CharField(max_length=255, db_index=True,
                             null=True, blank=True)

    # how many times the task has been tried
    attempts = models.IntegerField(default=0, db_index=True)
    # when the task last failed
    failed_at = models.DateTimeField(db_index=True, null=True, blank=True)
    # details of the error that occurred
    last_error = models.TextField(blank=True)

    # details of who's trying to run the task at the moment
    locked_by = models.CharField(max_length=64, db_index=True,
                                 null=True, blank=True)
    locked_at = models.DateTimeField(db_index=True, null=True, blank=True)

    creator_content_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.CASCADE)
    creator_object_id = models.PositiveIntegerField(null=True, blank=True)
    creator = GenericForeignKey('creator_content_type', 'creator_object_id')

    objects = TaskManager()

    def params(self):
        args, kwargs = json.loads(self.task_params)
        # need to coerce kwargs keys to str
        kwargs = dict((str(k), v) for k, v in kwargs.items())
        return args, kwargs

    def lock(self, locked_by):
        now = timezone.now()
        unlocked = Task.objects.unlocked(now).filter(pk=self.pk)
        updated = unlocked.update(locked_by=locked_by, locked_at=now)
        if updated:
            return Task.objects.get(pk=self.pk)
        return None

    def _extract_error(self, type, err, tb):
        file = StringIO()
        traceback.print_exception(type, err, tb, None, file)
        return file.getvalue()

    def increment_attempts(self):
        self.attempts += 1
        self.save()

    def has_reached_max_attempts(self):
        max_attempts = getattr(settings, 'MAX_ATTEMPTS', 25)
        return self.attempts >= max_attempts

    def is_repeating_task(self):
        return self.repeat > self.NEVER

    def reschedule(self, type, err, traceback):
        '''
        Set a new time to run the task in future, or create a CompletedTask and delete the Task
        if it has reached the maximum of allowed attempts
        '''
        self.last_error = self._extract_error(type, err, traceback)
        self.increment_attempts()
        if self.has_reached_max_attempts():
            self.failed_at = timezone.now()
            logging.warn('Marking task %s as failed', self)
            completed = self.create_completed_task()
            task_failed.send(sender=self.__class__, task_id=self.id, completed_task=completed)
            self.delete()
        else:
            backoff = timedelta(seconds=(self.attempts ** 4) + 5)
            self.run_at = timezone.now() + backoff
            logging.warn('Rescheduling task %s for %s later at %s', self,
                backoff, self.run_at)
            task_rescheduled.send(sender=self.__class__, task=self)
            self.locked_by = None
            self.locked_at = None
            self.save()

    def create_completed_task(self):
        '''
        Returns a new CompletedTask instance with the same values
        '''
        from background_task.models_completed import CompletedTask
        completed_task = CompletedTask(
            task_name=self.task_name,
            task_params=self.task_params,
            task_hash=self.task_hash,
            priority=self.priority,
            run_at=timezone.now(),
            queue=self.queue,
            attempts=self.attempts,
            failed_at=self.failed_at,
            last_error=self.last_error,
            locked_by=self.locked_by,
            locked_at=self.locked_at,
            verbose_name=self.verbose_name,
            creator=self.creator,
            repeat=self.repeat,
            repeat_until=self.repeat_until,
        )
        completed_task.save()
        return completed_task

    def create_repetition(self):
        """
        :return: A new Task with an offset of self.repeat, or None if the self.repeat_until is reached
        """
        if not self.is_repeating_task():
            return None

        if self.repeat_until <= timezone.now():
            # Repeat chain completed
            return None

        args, kwargs = self.params()
        new_run_at = self.run_at + timedelta(seconds=self.repeat)

        new_task = TaskManager().new_task(
            task_name=self.task_name,
            args=args,
            kwargs=kwargs,
            run_at=new_run_at,
            priority=self.priority,
            queue=self.queue,
            verbose_name=self.verbose_name,
            creator=self.creator,
            repeat=self.repeat,
            repeat_until=self.repeat_until,
        )
        new_task.save()
        return new_task

    def save(self, *arg, **kw):
        # force NULL rather than empty string
        self.locked_by = self.locked_by or None
        return super(Task, self).save(*arg, **kw)

    def __str__(self):
        return u'{}'.format(self.verbose_name or self.task_name)

    class Meta:
        db_table = 'background_task'
