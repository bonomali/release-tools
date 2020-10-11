import os
import shelve
from abc import ABC
from abc import abstractmethod
import re

import pathlib

RELEASE_REGEXP = re.compile(
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)\.?(?P<extra>.*)?"
)


class FiniteStateMachine:
    def __init__(self, *, release=None, git_repo=None, first_state=None):
        dbfile = pathlib.Path.home() / ".python_release"
        print(dbfile)
        self.db = shelve.open(str(dbfile), "c")
        if not self.db.get("finished"):
            self.db["finished"] = False
        else:
            self.db.close()
            self.db = shelve.open(str(dbfile), "n")

        self.current_task = first_state
        self.completed_tasks = self.db.get("completed_tasks", [])
        if self.db.get("gpg_key"):
            os.environ["GPG_KEY_FOR_RELEASE"] = self.db["gpg_key"]
        if not self.db.get("git_repo"):
            self.db["git_repo"] = pathlib.Path(git_repo)

        match = RELEASE_REGEXP.match(release)
        major = match.group("major")
        minor = match.group("minor")
        patch = match.group("patch")
        if not match:
            raise ValueError("Invalid release string")
        if not self.db.get("release"):
            self.db["release"] = release
        if not self.db.get("normalized_release"):
            self.db["normalized_release"] = f"{major}.{minor}.{patch}"
        if not self.db.get("release_branch"):
            branch = (
                f"{major}.{minor}.{patch}"
                if "extra" not in match.groupdict()
                else "master"
            )
            self.db["release_branch"] = branch

        print("Release data: ")
        print(f"- Branch: {self.db['release_branch']}")
        print(f"- Release tag: {self.db['release']}")
        print(f"- Normalized release tag: {self.db['normalized_release']}")
        print(f"- Git repo: {self.db['git_repo']}")
        print()

    def checkpoint(self):
        self.db["completed_tasks"] = self.completed_tasks
        self.db["last_checkpoint"] = self.current_task

    def load_checkpoint(self):
        self.current_task = self.db.get("last_checkpoint")
        return self.current_task

    def run(self):
        for task in self.completed_tasks:
            print(f"✅  {task.description}")

        while self.current_task is not None:
            self.checkpoint()
            current_state = self.current_task(self.db)
            try:
                result = current_state.run()
            except Exception as e:
                success = False
                print(f"\r💥  {current_state.description}")
                raise e from None
            print(f"\r✅  {current_state.description}")
            self.completed_tasks.append(self.current_task)
            self.current_task = current_state.next()

        self.db["finished"] = True


class Task(ABC):
    def __init__(self, db):
        self.db = db

    @abstractmethod
    def run(self):
        ...

    @abstractmethod
    def next(self):
        ...
