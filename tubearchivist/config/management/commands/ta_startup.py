"""
Functionality:
- Application startup
- Apply migrations
"""

import os
from random import randint
from time import sleep

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django_celery_beat.models import PeriodicTask
from home.models import CustomCronSchedule
from home.src.es.index_setup import ElasitIndexWrap
from home.src.es.snapshot import ElasticSnapshot
from home.src.ta.config import AppConfig, ReleaseVersion
from home.src.ta.helper import clear_dl_cache
from home.src.ta.settings import EnvironmentSettings
from home.src.ta.ta_redis import RedisArchivist
from home.src.ta.task_manager import TaskManager
from home.src.ta.users import UserConfig
from home.tasks import BaseTask

TOPIC = """

#######################
#  Application Start  #
#######################

"""


class Command(BaseCommand):
    """command framework"""

    # pylint: disable=no-member

    def handle(self, *args, **options):
        """run all commands"""
        self.stdout.write(TOPIC)
        self._sync_redis_state()
        self._make_folders()
        self._release_locks()
        self._clear_tasks()
        self._clear_dl_cache()
        self._version_check()
        self._mig_index_setup()
        self._mig_snapshot_check()
        self._mig_move_users_to_es()
        self._mig_schedule_store()

    def _sync_redis_state(self):
        """make sure redis gets new config.json values"""
        self.stdout.write("[1] set new config.json values")
        needs_update = AppConfig().load_new_defaults()
        if needs_update:
            self.stdout.write(
                self.style.SUCCESS("    ✓ new config values set")
            )
        else:
            self.stdout.write(self.style.SUCCESS("    no new config values"))

    def _make_folders(self):
        """make expected cache folders"""
        self.stdout.write("[2] create expected cache folders")
        folders = [
            "backup",
            "channels",
            "download",
            "import",
            "playlists",
            "videos",
        ]
        cache_dir = EnvironmentSettings.CACHE_DIR
        for folder in folders:
            folder_path = os.path.join(cache_dir, folder)
            os.makedirs(folder_path, exist_ok=True)

        self.stdout.write(self.style.SUCCESS("    ✓ expected folders created"))

    def _release_locks(self):
        """make sure there are no leftover locks set in redis"""
        self.stdout.write("[3] clear leftover locks in redis")
        all_locks = [
            "dl_queue_id",
            "dl_queue",
            "downloading",
            "manual_import",
            "reindex",
            "rescan",
            "run_backup",
            "startup_check",
        ]

        redis_con = RedisArchivist()
        has_changed = False
        for lock in all_locks:
            if redis_con.del_message(lock):
                self.stdout.write(
                    self.style.SUCCESS(f"    ✓ cleared lock {lock}")
                )
                has_changed = True

        if not has_changed:
            self.stdout.write(self.style.SUCCESS("    no locks found"))

    def _clear_tasks(self):
        """clear tasks and messages"""
        self.stdout.write("[4] clear task leftovers")
        TaskManager().fail_pending()
        redis_con = RedisArchivist()
        to_delete = redis_con.list_keys("message:")
        if to_delete:
            for key in to_delete:
                redis_con.del_message(key)

            self.stdout.write(
                self.style.SUCCESS(f"    ✓ cleared {len(to_delete)} messages")
            )

    def _clear_dl_cache(self):
        """clear leftover files from dl cache"""
        self.stdout.write("[5] clear leftover files from dl cache")
        leftover_files = clear_dl_cache(EnvironmentSettings.CACHE_DIR)
        if leftover_files:
            self.stdout.write(
                self.style.SUCCESS(f"    ✓ cleared {leftover_files} files")
            )
        else:
            self.stdout.write(self.style.SUCCESS("    no files found"))

    def _version_check(self):
        """remove new release key if updated now"""
        self.stdout.write("[6] check for first run after update")
        new_version = ReleaseVersion().is_updated()
        if new_version:
            self.stdout.write(
                self.style.SUCCESS(f"    ✓ update to {new_version} completed")
            )
        else:
            self.stdout.write(self.style.SUCCESS("    no new update found"))

    def _mig_index_setup(self):
        """migration: validate index mappings"""
        self.stdout.write("[MIGRATION] validate index mappings")
        ElasitIndexWrap().setup()

    def _mig_snapshot_check(self):
        """migration setup snapshots"""
        self.stdout.write("[MIGRATION] setup snapshots")
        ElasticSnapshot().setup()

    def _mig_move_users_to_es(self):  # noqa: C901
        """migration: update from 0.4.1 to 0.4.2 move user config to ES"""
        self.stdout.write("[MIGRATION] move user configuration to ES")
        redis = RedisArchivist()

        # 1: Find all users in Redis
        users = {i.split(":")[0] for i in redis.list_keys("[0-9]*:")}
        if not users:
            self.stdout.write("    no users needed migrating to ES")
            return

        # 2: Write all Redis user settings to ES
        # 3: Remove user settings from Redis
        try:
            for user in users:
                new_conf = UserConfig(user)

                stylesheet_key = f"{user}:color"
                stylesheet = redis.get_message(stylesheet_key).get("status")
                if stylesheet:
                    new_conf.set_value("stylesheet", stylesheet)
                    redis.del_message(stylesheet_key)

                sort_by_key = f"{user}:sort_by"
                sort_by = redis.get_message(sort_by_key).get("status")
                if sort_by:
                    new_conf.set_value("sort_by", sort_by)
                    redis.del_message(sort_by_key)

                page_size_key = f"{user}:page_size"
                page_size = redis.get_message(page_size_key).get("status")
                if page_size:
                    new_conf.set_value("page_size", page_size)
                    redis.del_message(page_size_key)

                sort_order_key = f"{user}:sort_order"
                sort_order = redis.get_message(sort_order_key).get("status")
                if sort_order:
                    new_conf.set_value("sort_order", sort_order)
                    redis.del_message(sort_order_key)

                grid_items_key = f"{user}:grid_items"
                grid_items = redis.get_message(grid_items_key).get("status")
                if grid_items:
                    new_conf.set_value("grid_items", grid_items)
                    redis.del_message(grid_items_key)

                hide_watch_key = f"{user}:hide_watched"
                hide_watch = redis.get_message(hide_watch_key).get("status")
                if hide_watch:
                    new_conf.set_value("hide_watched", hide_watch)
                    redis.del_message(hide_watch_key)

                ignore_only_key = f"{user}:show_ignored_only"
                ignore_only = redis.get_message(ignore_only_key).get("status")
                if ignore_only:
                    new_conf.set_value("show_ignored_only", ignore_only)
                    redis.del_message(ignore_only_key)

                subed_only_key = f"{user}:show_subed_only"
                subed_only = redis.get_message(subed_only_key).get("status")
                if subed_only:
                    new_conf.set_value("show_subed_only", subed_only)
                    redis.del_message(subed_only_key)

                for view in ["channel", "playlist", "home", "downloads"]:
                    view_key = f"{user}:view:{view}"
                    view_style = redis.get_message(view_key).get("status")
                    if view_style:
                        new_conf.set_value(f"view_style_{view}", view_style)
                        redis.del_message(view_key)

                self.stdout.write(
                    self.style.SUCCESS(
                        f"    ✓ Settings for user '{user}' migrated to ES"
                    )
                )
        except Exception as err:
            message = "    🗙 user migration to ES failed"
            self.stdout.write(self.style.ERROR(message))
            self.stdout.write(self.style.ERROR(err))
            sleep(60)
            raise CommandError(message) from err
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "    ✓ Settings for all users migrated to ES"
                )
            )

    def _mig_schedule_store(self):
        """
        update from 0.4.4 to 0.4.5
        migrate schedule task store to CustomCronSchedule
        """
        self.stdout.write("[MIGRATION] migrate schedule store")
        config = AppConfig().config
        current_schedules = config.get("scheduler")
        if not current_schedules:
            self.stdout.write(
                self.style.SUCCESS("    no schedules to migrate")
            )
            return

        self._mig_update_subscribed(current_schedules)
        self._mig_download_pending(current_schedules)
        self._mig_check_reindex(current_schedules)
        self._mig_thumbnail_check(current_schedules)
        self._mig_run_backup(current_schedules)
        self._mig_version_check()

        del config["scheduler"]
        RedisArchivist().set_message("config", config, save=True)

    def _mig_update_subscribed(self, current_schedules):
        """create update_subscribed schedule"""
        update_subscribed_schedule = current_schedules.get("update_subscribed")
        if update_subscribed_schedule:
            task_config = False
            notify = current_schedules.get("update_subscribed_notify")
            if notify:
                task_config = {"notify": notify}
            self._create_task(
                "update_subscribed",
                update_subscribed_schedule,
                task_config=task_config,
            )

    def _mig_download_pending(self, current_schedules):
        """create download_pending schedule"""
        download_pending_schedule = current_schedules.get("download_pending")
        if download_pending_schedule:
            task_config = False
            notify = current_schedules.get("download_pending_notify")
            if notify:
                task_config = {"notify": notify}
            self._create_task(
                "download_pending",
                download_pending_schedule,
                task_config=task_config,
            )

    def _mig_check_reindex(self, current_schedules):
        """create check_reindex schedule"""
        check_reindex_schedule = current_schedules.get("check_reindex")
        if check_reindex_schedule:
            task_config = {}
            notify = current_schedules.get("check_reindex_notify")
            if notify:
                task_config.update({"notify": notify})

            days = current_schedules.get("check_reindex_days")
            if days:
                task_config.update({"days": days})

            self._create_task(
                "check_reindex",
                check_reindex_schedule,
                task_config=task_config,
            )

    def _mig_thumbnail_check(self, current_schedules):
        """create thumbnail_check schedule"""
        thumbnail_check_schedule = current_schedules.get("thumbnail_check")
        if thumbnail_check_schedule:
            self._create_task("thumbnail_check", thumbnail_check_schedule)

    def _mig_run_backup(self, current_schedules):
        """create run_backup schedule"""
        run_backup_schedule = current_schedules.get("run_backup")
        if run_backup_schedule:
            task_config = False
            rotate = current_schedules.get("run_backup_rotate")
            if rotate:
                task_config = {"rotate": rotate}

            self._create_task(
                "run_backup", run_backup_schedule, task_config=task_config
            )

    def _mig_version_check(self):
        """create version_check schedule"""
        version_check_schedule = {
            "minute": randint(0, 59),
            "hour": randint(0, 23),
            "day_of_week": "*",
        }
        self._create_task("version_check", version_check_schedule)

    def _create_task(self, task_name, schedule, task_config=False):
        """create task"""
        description = BaseTask.TASK_CONFIG[task_name].get("title")
        schedule, _ = CustomCronSchedule.objects.get_or_create(**schedule)
        schedule.timezone = settings.TIME_ZONE
        if task_config:
            schedule.task_config = task_config

        schedule.save()

        task, _ = PeriodicTask.objects.get_or_create(
            crontab=schedule,
            name=task_name,
            description=description,
            task=f"home.tasks.{task_name}",
        )
        self.stdout.write(
            self.style.SUCCESS(f"    ✓ new task created: '{task}'")
        )
