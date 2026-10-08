from app.plugin_api import migration_env

from example_plugin.models import Base

migration_env(Base.metadata, "example")
