import os
import datetime
from typing import Optional
from pathlib import Path

class Config:
    """
    Holds configuration for the CLI, scanning, storage, and email.
    """
    def __init__(
        self,
        db_name: str,
        email: str,
        log_directory: str,
        days: int,
        user_name: str,
        suite_name: str,
        branch_name: str,
        flavor: str,
        error_message: Optional[str] = None,
        keep_db: bool = False,
        bot: bool = False,
        verbose: bool = False,
    ) -> None:
        self.db_name = db_name
        self.email = email
        self.log_directory = Path(log_directory)
        self.days = days
        self.user_name = user_name
        self.suite_name = suite_name
        self.branch_name = branch_name
        self.flavor = flavor
        self.error_message = error_message
        self.keep_db = keep_db
        self.bot = bot
        self.verbose = verbose

        # supported versions and flavors
        self.versions = ['quincy', 'squid', 'main', 'reef', 'tentacle']
        self.flavors = ['default']
        #self.flavors  = ['default', 'crimson']
        self.bot_users = ['bharath', 'teuthology', 'yuriw','skanta']

        self.redmine_config_path  = os.environ.get('REDMINE_CONFIG', '~/.redmine')
        cache_file = 'tracker_cache.json'
        cache_env = os.environ.get("TRACKER_CACHE")
        cache_path = Path(cache_env).expanduser() if cache_env else Path(cache_file).expanduser()
        if not cache_path.is_absolute():
            cache_path = Path(__file__).resolve().parent / cache_path
        self.tracker_cache_file = cache_path
        

        # Output directory for graphs/images
        self.output_dir = os.environ.get('OUTPUT_DIR', os.getcwd())

        # SMTP/email settings
        self.smtp_server   = os.environ.get('SMTP_SERVER', 'localhost')
        self.smtp_port     = int(os.environ.get('SMTP_PORT', 25))
        self.smtp_username = os.environ.get('SMTP_USERNAME', '')
        self.smtp_password = os.environ.get('SMTP_PASSWORD', '')
        self.email_sender  = 'watcher@teuthology.com'

    @classmethod
    def from_args(cls, args) -> 'Config':
        """
        Construct Config from argparse Namespace.
        """
        return cls(
            db_name=args.db_name,
            email=args.email,
            log_directory=args.log_directory,
            days=args.days,
            user_name=args.user_name,
            suite_name=args.suite_name,
            branch_name=args.branch_name,
            flavor=args.flavor,
            error_message=getattr(args, 'error_message', None),
            keep_db=args.keep_db,
            bot=args.bot,
            verbose=args.verbose,
        )
