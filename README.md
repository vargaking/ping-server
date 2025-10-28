# FastAPI Skeleton

Generic skeleton for a FastAPI application using Uvicorn for development and Gunicorn for production (with Uvicorn workers).

This skel works as-is: you can clone it and run it and it will work and pass all tests :)

To make your life easier, this skeleton comes also a series of automated tasks using `invoke`, if you don't know it check its [documentation](https://www.pyinvoke.org/).  
For example you can run `inv lint` to lint your code with `flake8`, `pydocstyle`, `bandit` and `hadolint`. To view all of the tasks check the file `tasks.py` or run `inv -l`.  
It comes with lots of flake plugins so adjust to taste. To view all of the flake plugins and other libraries included check the `pyproject.toml` file.

When you have all set up you can run your application with `inv runserver --development` and have fun :). Or run tests with `inv tests --coverage` to also get the coverage report.

## Getting Started
 
These instructions will guide you on how to use this skeleton.

We haven't used any scaffolding utilities so we kept this skel quite basic. The idea is to grab it and mold it to your needs without having to adapt your app to the skel, but otherwise.

### First steps

First clone this repo: `git clone git@gitlab.com:nevrona/public/skels/fastapi.git`

Rename the `app_skel` directory to your project's name, and the `app_skel` directory under `static`, and after that search and replace `app_skel` with it. Here's a list of files needing change, but using your IDE/editor *search-and-replace* is safer in case we missed one:

* pyproject.toml
* tasks.py
* .flake8
* .gitlab-ci.yml
* Dockerfile
* docker-entrypoint.ash
 
 Then set your application environment prefix (i.e. a three characters code), which is currently `AEP_` in `conf/global_settings.py:ENV_PREFIX` and rename the testing environment variable in `.gitlab-ci.yml:AEP_TESTING` accordingly. The prefix can be empty: it's simply used to prepend to the settings environment variables.

From the functional point of view, that's it :). You may want to write something descriptive at the package's `__init__.py` file (`app_skel/__init__.py`).

However, if you plan to distribute your code based on this skel, you **must** change the vendor in `Dockerfile:LABEL org.opencontainers.image.vendor` and other than that comply with the [license](#license).

## Structure

This skel is to be used with a versioned API. The structure is as follows:

* `api`: This module contains all API related modules at the upper level and all the endpoints/views, business logic and so on contained in a defined version.
   * `root` file: Contains API root unversioned views.
   * `routing` file: Contains helpers and internal functions related to API routes.
   * `shortcuts` file: Contains helpers related to database handling from views.
   * `vX_Y`: API version submodule. It can be called anyway as long as it begins with `v`. Currently there's no way to remove the version with the autorouting, so for that case you would need to route it manually.
      * `urls` file: Contains magic autorouter logic for the current api version. It generates all routes for the endpoints. Remove or replace to avoid autorouting.
      * `endpoints`: This module contains API endpoints with views. Each view URL is prepended with the file name, so for an endpoint named `users` with a view like `/verify` the URL is translated as `.../users/verify` by the autorouting (files beginning with underscore (`_`) or submodules are ignored).
      * `selectors`: This module should contain business logic around fetching data from the database.
      * `services`: This module should contain business logic around storing data and other complex, cross-cutting concerns.
* `app`: This module configures and initializes the FastAPI ASGI application. Import `app` or `application` from it.
   * `asgi` file: It instantiates the application. There shouldn't be a need to add things here.
   * `events` file: It contains application async events such as startup and shutdown. Write your events logic here.
   * `fastapi` file: It contains logic to create the application in a function.
   * `middlewares` file: Contains the application middlewares. Add your middlewares here.
   * `router` file: Contains application routes instantiation. It uses the autorouting function which you can remove to disable such functionality. Manually add routes here in that case.
   * `routing` file: Contains internal application routing utilities. You may edit the routes configuration function but there shouldn't be any need if you're using the autorouting capabilities.
* `conf`: This module handles settings. Import `settings` from this module from anywhere in your app. Currently all settings lives in `global_settings`.
   * `gunicorn` file: Contains generic settings to be used by [gunicorn](https://gunicorn.org/) only.
   * `settings_for_tests` file: Contains all settings that need to be fixed/set when running tests.
   * `local_settings.sample` file: This file is populated with some default settings useful for local development. Any other sample file can be included for other environments but it is recommended to use environment variables or dotenv files instead. This sample file must be renamed to `local_settings.py` for the settings system to use it.
* `crud`: This module handles database CRUD operations and has a handy base class for a generic CRUD.
* `db`: This module handles database initialization. You can import `SessionLocal` to obtain a session but from views use `Depends(get_db)` [as documented](https://fastapi.tiangolo.com/tutorial/sql-databases/#create-a-dependency) (see `api.shortcuts`).
* `models`: This module contains models and handles models initialization. Import and expose models in `__init__`.
* `schemas`: This module contains Pydantic schemas.
* `static`: This directory contains static files to serve. Use nginx or similar to serve them in production. For convenience, all assets are under a directory named as the project. Use `api.routing.get_static()` to get the URL for a static asset.
* `tests`: This module contains all the tests for the application.
* `utils`: This module contains generic utilities such as base64 converters, a useful JSONEncoder, etc. Remove any unwanted file as needed.

### Views and business logic

Views live inside the `api` module, under a defined api version inside the `endpoints` module. Business logic should live under `services` and helpers/data-getters under `selectors`.

### Settings

Add all the settings you need to in `conf/global_settings.py` which is the main settings file. We are currently using [Pydantic's BaseSettings](https://pydantic-docs.helpmanual.io/usage/settings/) which comes with free validation, environment and dotenv support (for the later install `pydantic[dotenv]`). Make sure to set any required setting in `conf/settings_for_tests.py` too so that they are fixed when running tests.

If you need to add additional checks regarding business logic with settings see `conf/checks.py:configure_settings` and add whatever you need there (remember to update corresponding tests!).

To use settings from anywhere in the app, simply import *settings*: `from app_skel.conf import settings`. The inner function uses LRU cache to efficiently cache the settings values. Its type is `Settings` which can be imported from `conf` too. Additionally you can get settings in views as a FastAPI dependency (as seen in [the manual](https://fastapi.tiangolo.com/advanced/settings/#settings-in-a-dependency)):
 
 ```python
from fastapi import Depends
from app_skel.conf import get_settings, Settings

async def view(settings: Settings = Depends(get_settings)):
    ...
```

### Tests

Simply run `inv tests` and optionally `inv tests -c` for coverage report. We're using `nose2` as test collector, `coverage` for coverage reports and `unittest` for everything else.

To run tests the app needs special settings in place with are in `conf/settings_for_tests.py`. Add there any setting you require.

The system knows when to use test settings because of the environment variable `TESTING` (*with the corresponding prefix*). Set `TESTING` to `true` or `1` for the system to load test settings. That's it.

### No database

If you are not planning to use a database, remove the following modules:

* crud
* db
* models

And the *shortcuts* file in the *api* module and the settings tagged as database from the `global_settings` file. Also remove the `DBSessionMiddleware` from `app.middlewares`. Finally run `poetry remove sqlalchemy sqlalchemy-utils`.

## License

*FastAPI Skeleton* is made by [Nevrona S. A.](https://nevrona.org) under `MPL-2.0`. You are free to use, share, modify and share modifications under the terms of that [license](LICENSE). Derived works may link back to the canonical repository: `https://gitlab.com/nevrona/public/skels/fastapi`.

**Neither the name of Nevrona S. A. nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.**
