import asyncio
import json
from dataclasses import dataclass

from .logparser import Logs


@dataclass
class Builder:
    id: int

    def __init__(self, **kwargs):
        self.__dict__.update(**kwargs)
        self.id = kwargs.get("builderid")

    def __hash__(self):
        return hash(self.id)


@dataclass
class Build:
    id: int
    is_currently_failing: bool
    logs: Logs

    def __init__(self, **kwargs):
        self.__dict__.update(**kwargs)
        self.id = kwargs.get("number")
        self.is_currently_failing = kwargs.get("currently_failing")
        self.builder = None
        self.logs = None

    def __eq__(self, other):
        return self.id == other.id

    def __hash__(self):
        return hash(self.id)


@dataclass
class Change:
    sha: str

    def __init__(self, **kwargs):
        self.sha = kwargs.get("revision")
        self.__dict__.update(**kwargs)

    def __eq__(self, other):
        return self.sha == other.sha

    def __hash__(self):
        return hash(self.sha)


class BuildBotAPI:
    def __init__(self, session):
        self._session = session

        self._stable_builders = None

    async def authenticate(self, token):
        await self._session.get(
            "https://buildbot.python.org/all/auth/login", params={"token": token}
        )

    async def _fetch_text(self, url):
        async with self._session.get(url) as resp:
            return await resp.text()

    async def _fetch_json(self, url):
        return json.loads(await self._fetch_text(url))

    async def _get_stable_builders(self, branch):
        stable_builders = {
            id: builder
            for (id, builder) in (await self.all_builders(branch=branch)).items()
            if "stable" in builder.tags
        }
        if self._stable_builders is None:
            self._stable_builders = stable_builders
        return stable_builders

    async def all_builders(self, branch=None):
        url = "https://buildbot.python.org/all/api/v2/builders"
        if branch:
            url = f"{url}?tags__contains={branch}"
        builders = await self._fetch_json(url)
        builders = builders["builders"]
        all_builders = {
            builder["builderid"]: Builder(**builder) for builder in builders
        }
        return all_builders

    async def stable_builders(self, branch=None):
        stable_builders = self._stable_builders
        if stable_builders is None:
            stable_builders = await self._get_stable_builders(branch=branch)
        return stable_builders

    async def is_builder_failing_currently(self, builder):
        builds = await self._fetch_json(
            f"https://buildbot.python.org/all/api/v2/builds?complete__eq=true"
            f"&&builderid__eq={builder.id}&&order=-complete_at"
            f"&&limit=1"
        )
        builds = builds["builds"]
        if not builds:
            return False
        (build,) = builds
        if build["results"] == 2:
            return True
        return False

    async def get_logs_for_last_build(self, builder):
        builds = await self._fetch_json(
            f"https://buildbot.python.org/all/api/v2/builds?complete__eq=true"
            f"&&builderid__eq={builder.id}&&order=-complete_at"
            f"&&limit=1"
        )
        builds = builds["builds"]
        if not builds:
            return Logs("")
        (build,) = builds
        data = await self._fetch_json(
            f"https://buildbot.python.org/all/api/v2/builders/{builder.id}"
            f"/builds/{build['number']}/steps/test/logs"
        )
        if not data["logs"]:
            return Logs("")
        logid = data["logs"][0]["logid"]
        return Logs(
            await self._fetch_text(
                f"https://buildbot.python.org/all/api/v2/logs/{logid}/raw"
            )
        )

    async def get_logs(self, build):
        data = await self._fetch_json(
            f"https://buildbot.python.org/all/api/v2/builders/{build.builder.id}"
            f"/builds/{build.id}/steps/test/logs"
        )
        if not data["logs"]:
            return ""
        logid = data["logs"][0]["logid"]
        return await self._fetch_text(
            f"https://buildbot.python.org/all/api/v2/logs/{logid}/raw"
        )

    async def get_build(self, builder_id, build_id):
        data = await self._fetch_json(
            f"https://buildbot.python.org/all/api/v2/builders/{builder_id}"
            f"/builds/{build_id}"
        )
        (build_data,) = data["builds"]
        build = Build(**build_data)
        build.builder = (await self.all_builders())[build.builderid]
        build.is_currently_failing = await self.is_builder_failing_currently(
            build.builder
        )
        build.logs = Logs(await self.get_logs(build))
        build.changes = await self._get_changes_for_build(build)
        return build

    async def _get_changes_for_build(self, build):
        data = await self._fetch_json(
            f"https://buildbot.python.org/all/api/v2/builds/{build.buildid}/changes"
        )
        return [Change(**change) for change in data["changes"]]

    async def get_recent_failures(self, limit=100):
        data = await self._fetch_json(
            f"https://buildbot.python.org/all/api/v2/builds?"
            f"complete__eq=true&&results__eq=2&&"
            f"order=-complete_at&&limit={limit}"
        )

        stable_builders = await self.stable_builders()

        all_failures = {
            Build(**build)
            for build in data["builds"]
            if build["builderid"] in stable_builders
        }

        for failure in all_failures:
            failure.builder = stable_builders[failure.builderid]

        async def _get_missing_info(failure):
            failure.is_currently_failing = await self.is_builder_failing_currently(
                failure.builder
            )
            failure.logs = Logs(await self.get_logs(failure))
            failure.changes = await self._get_changes_for_build(failure)

        await asyncio.gather(*[_get_missing_info(failure) for failure in all_failures])

        return all_failures
