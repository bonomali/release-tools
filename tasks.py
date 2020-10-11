import asyncio
import os
import contextlib
import pathlib
import builtins
import tempfile

import aiohttp
import paramiko
import gnupg
import shutil
import subprocess

from libbuildbot.buildbotapi import BuildBotAPI
import release
from alive_progress import alive_bar
import time


from automata import FiniteStateMachine, Task


DOWNLOADS_SERVER = "downloads.nyc1.psf.io"

class ReleaseException(Exception):
    ...


def ask_question(question):
    answer = ""
    print(question)
    while answer not in ("yes", "no"):
        answer = input("Enter yes or no: ")
        if answer == "yes":
            return True
        elif answer == "no":
            return False
        else:
            print("Please enter yes or no.")


@contextlib.contextmanager
def cd(path):
    current_path = os.getcwd()
    os.chdir(path)
    yield
    os.chdir(current_path)


@contextlib.contextmanager
def supress_print():
    print_func = builtins.print
    builtins.print = lambda *args, **kwargs: None
    yield
    builtins.print = print_func


class check_buildbots(Task):
    description = "Check buildbots are good"

    def run(self):
        async def _check():
            async def _get_builder_status(api: BuildBotAPI, builder):
                return builder, await api.is_builder_failing_currently(builder)

            async with aiohttp.ClientSession() as session:
                api = BuildBotAPI(session)
                await api.authenticate(token="")
                stable_builders = await api.stable_builders(
                    branch=self.db["release_branch"]
                )
                builders = await asyncio.gather(
                    *[
                        _get_builder_status(api, builder)
                        for builder in stable_builders.values()
                    ]
                )
                return {
                    builder: await api.get_logs_for_last_build(builder)
                    for (builder, is_failing) in builders
                    if is_failing
                }

        failing_builders = asyncio.run(_check())
        if not failing_builders:
            return
        print("The following buildbots are failing:")
        for builder, logs in failing_builders.items():
            print(f"- {builder.name}")
            print(f"\t\t{logs.test_summary()}")
        print(
            "Check https://buildbot.python.org/all/#/release_status for more information"
        )
        if not ask_question(
            "Do you want to continue even if these builders are failing?"
        ):
            raise ReleaseException("Buildbots are failing!")

    def next(self):
        return run_blurb_release


class check_ssh_connection(Task):
    description = f"Validating ssh connection to {DOWNLOADS_SERVER}"

    def run(self):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.WarningPolicy)
        client.connect(DOWNLOADS_SERVER, port=22)
        stdin, stdout, stderr = client.exec_command("pwd")

    def next(self):
        return check_buildbots


class check_gpg_keys(Task):
    description = "Checking GPG keys"

    def run(self):
        pg = gnupg.GPG()
        keys = pg.list_keys(secret=True)
        if not keys:
            raise ReleaseException("There are no valid GPG keys for release")
        for index, key in enumerate(keys):
            print(f"{index} - {key['keyid']}: {key['uids']}")
        selected_key_index = int(input("Select one GPG key for release (by index):"))
        selected_key = keys[selected_key_index]['keyid']
        os.environ["GPG_KEY_FOR_self.db['release']"] = selected_key
        if selected_key not in {key["keyid"] for key in keys}:
            raise ReleaseException("Invalid GPG key selected")
        self.db["gpg_key"] = selected_key

    def next(self):
        return check_ssh_connection


class check_git(Task):
    description = "Checking git is available"

    def run(self):
        return shutil.which("git")

    def next(self):
        return check_make


class check_blurb(Task):
    description = "Checking blurb is available"

    def run(self):
        return shutil.which("blurb")

    def next(self):
        return check_autoconf


class check_make(Task):
    description = "Checking make is available"

    def run(self):
        return shutil.which("make")

    def next(self):
        return check_blurb


class check_autoconf(Task):
    description = "Checking autoconf is available"

    def run(self):
        return shutil.which("autoconf")

    def next(self):
        return check_gpg_keys


class check_cpython_repo_is_clean(Task):
    description = "Checking git repository is clean"

    def run(self):
        if subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=self.db["git_repo"]
        ):
            raise ReleaseException("Git repository is not clean")

    def next(self):
        ...


class run_blurb_release(Task):
    description = "Run blurb release"

    def run(self):
        check_cpython_repo_is_clean(self.db).run()
        subprocess.check_call(
            ["blurb", "release", self.db["release"]], cwd=self.db["git_repo"]
        )
        subprocess.check_call(
            ["git", "commit", "-m", f"Python {self.db['release']}"],
            cwd=self.db["git_repo"],
        )

    def next(self):
        return check_docs


class check_docs(Task):
    description = "Check documentation"

    def run(self):
        subprocess.check_call(["make", "venv"], cwd=self.db["git_repo"] / "Doc")
        subprocess.check_call(
            ["make", "suspicious"],
            cwd=self.db["git_repo"] / "Doc",
            env={**os.environ, "SPHINXOPTS": "-j10"},
        )

    def next(self):
        return prepare_pydoc_topics


class prepare_pydoc_topics(Task):
    description = "Preparing pydoc topics"

    def run(self):
        check_cpython_repo_is_clean(self.db).run()
        subprocess.check_call(["make", "pydoc-topics"], cwd=self.db["git_repo"] / "Doc")
        shutil.copy2(
            self.db["git_repo"] / "Doc" / "build" / "pydoc-topics" / "topics.py",
            self.db["git_repo"] / "Lib" / "pydoc_data" / "topics.py",
        )
        subprocess.check_call(
            ["git", "commit", "-a", "--amend", "--no-edit"], cwd=self.db["git_repo"]
        )

    def next(self):
        return run_autoconf


class run_autoconf(Task):
    description = "Running autoconf"

    def run(self):
        subprocess.check_call(["autoconf"], cwd=self.db["git_repo"])
        subprocess.check_call(
            ["git", "commit", "-a", "--amend", "--no-edit"], cwd=self.db["git_repo"]
        )

    def next(self):
        return check_pyspecific


class check_pyspecific(Task):
    description = "Checking pyspecific"

    def run(self):
        with open(
            self.db["git_repo"] / "Doc" / "tools" / "extensions" / "pyspecific.py", "r"
        ) as pyspecific:
            for line in pyspecific:
                if "SOURCE_URI =" in line:
                    break
        expected = f"SOURCE_URI = 'https://github.com/python/cpython/tree/{self.db['release_branch']}/%s'"
        if expected != line.strip():
            raise ReleaseException("SOURCE_URI is incorrect")

    def next(self):
        return bump_version


class bump_version(Task):
    description = "Bump version"

    def run(self):
        check_cpython_repo_is_clean(self.db).run()
        with cd(self.db["git_repo"]):
            release.bump(release.Tag(self.db["release"]))
        subprocess.check_call(
            ["git", "commit", "-a", "--amend", "--no-edit"], cwd=self.db["git_repo"]
        )

    def next(self):
        return create_tag


class create_tag(Task):
    description = "Create tag"

    def run(self):
        check_cpython_repo_is_clean(self.db).run()
        with cd(self.db["git_repo"]):
            if not release.make_tag(release.Tag(self.db["release"])):
                raise ReleaseException("Error when creating tag")
        subprocess.check_call(
            ["git", "commit", "-a", "--amend", "--no-edit"], cwd=self.db["git_repo"]
        )

    def next(self):
        return build_release_artifacts


class build_release_artifacts(Task):
    description = "Building release artifacts"

    def run(self):
        with cd(self.db["git_repo"]):
            release.export(release.Tag(self.db["release"]))

    def next(self):
        return test_release_artifacts


class test_release_artifacts(Task):
    description = "Test release artifacts"

    def run(self):
        with tempfile.TemporaryDirectory() as the_dir:
            the_dir = pathlib.Path(the_dir)
            the_dir.mkdir(exist_ok=True)
            filename = f"Python-{self.db['release']}"
            tarball = f"Python-{self.db['release']}.tgz"
            shutil.copy2(
                self.db["git_repo"] / self.db["release"] / "src" / tarball,
                the_dir / tarball,
            )
            subprocess.check_call(["tar", "xvf", tarball], cwd=the_dir)
            subprocess.check_call(
                ["./configure", "--prefix", str(the_dir / "installation")],
                cwd=the_dir / filename,
            )
            subprocess.check_call(["make", "-j"], cwd=the_dir / filename)
            subprocess.check_call(["make", "install"], cwd=the_dir / filename)
            process = subprocess.run(
                ["./bin/python3", "-m", "test", "test_list"],
                cwd=str(the_dir / "installation"),
                text=True,
            )

        if process.returncode == 0:
            return True
        if not ask_question("Some test_failed! Do you want to continue?"):
            raise ReleaseException("Test failed!")

    def next(self):
        return upload_files_to_server


class upload_files_to_server(Task):
    description = "Upload files to the PSF server"

    class MySFTPClient(paramiko.SFTPClient):
        def put_dir(self, source, target, progress=None):
            for item in os.listdir(source):
                if os.path.isfile(os.path.join(source, item)):
                    progress.text(item)
                    self.put(os.path.join(source, item), "%s/%s" % (target, item))
                    progress()
                else:
                    self.mkdir("%s/%s" % (target, item), ignore_existing=True)
                    self.put_dir(
                        os.path.join(source, item),
                        "%s/%s" % (target, item),
                        progress=progress,
                    )

        def mkdir(self, path, mode=511, ignore_existing=False):
            try:
                super().mkdir(path, mode)
            except IOError:
                if ignore_existing:
                    pass
                else:
                    raise

    def run(self):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.WarningPolicy)
        client.connect(DOWNLOADS_SERVER, port=22)

        destination = f"/home/psf-users/pablogsal/{self.db['release']}"
        ftp_client = self.MySFTPClient.from_transport(client.get_transport())

        client.exec_command(f"rm -rf {destination}")

        with contextlib.suppress(OSError):
            ftp_client.mkdir(destination)

        with alive_bar(
            len(
                tuple(
                    pathlib.Path(self.db["git_repo"] / self.db["release"] / "src").glob(
                        "**/*"
                    )
                )
            )
        ) as progress:
            ftp_client.put_dir(
                self.db["git_repo"] / self.db["release"] / "src",
                f"/home/psf-users/pablogsal/{self.db['release']}",
                progress=progress,
            )
        ftp_client.close()

    def next(self):
        return place_files_in_download_folder


class place_files_in_download_folder(Task):
    description = "Place files in the download folder"

    def run(self):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.WarningPolicy)
        client.connect(DOWNLOADS_SERVER, port=22)

        source = f"/home/psf-users/pablogsal/{self.db['release']}"
        destination = f"/srv/www.python.org/ftp/python/{self.db['normalized_release']}"

        def execute_command(command):
            channel = client.get_transport().open_session()
            channel.exec_command(command)
            if channel.recv_exit_status() != 0:
                raise ReleaseException(channel.recv_stderr(1000))

        execute_command(f"mkdir -p {destination}")
        execute_command(f"cp {source}/* {destination}")
        execute_command(f"chgrp downloads {destination}")
        execute_command(f"chmod 775 {destination}")
        execute_command(f"find {destination} -type f -exec chmod 664 {{}} \;")

    def next(self):
        return send_email_to_platform_release_managers


class send_email_to_platform_release_managers(Task):
    description = (
        "Platform release managers have been notified of the release artifacts"
    )

    def run(self):
        if not ask_question(
            "Have you notified the platform release managers about the availability of artifacts?"
        ):
            raise ReleaseException("Platform release managers muy be notified")

    def next(self):
        return create_release_object_in_db


class create_release_object_in_db(Task):
    description = "The django release object has been created"

    def run(self):
        print(
            "Go to https://www.python.org/admin/downloads/release/add/ and create a new release"
        )
        if not ask_question(
            f"Have you already created a new release for {self.db['release']}"
        ):
            raise ReleaseException("The django release object has not been created")

    def next(self):
        return wait_util_all_files_are_in_folder


class wait_util_all_files_are_in_folder(Task):
    description = "Wait until all files are ready"

    def run(self):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.WarningPolicy)
        client.connect(DOWNLOADS_SERVER, port=22)
        ftp_client = client.open_sftp()

        destination = f"/srv/www.python.org/ftp/python/{self.db['normalized_release']}"

        are_all_files_there = False
        print()
        while not are_all_files_there:
            try:
                all_files = ftp_client.listdir(destination)
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"The release folder in {destination} has not been created"
                ) from None
            are_windows_files_there = any(file.endswith(".exe") for file in all_files)
            are_macos_files_there = any(file.endswith(".pkg") for file in all_files)
            are_linux_files_there = any(file.endswith(".tgz") for file in all_files)
            are_all_files_there = (
                are_linux_files_there
                and are_windows_files_there
                and are_macos_files_there
            )
            if not are_macos_files_there:
                linux_tick = "✅" if are_linux_files_there else "❎"
                windows_tick = "✅" if are_windows_files_there else "❎"
                macos_tick = "✅" if are_macos_files_there else "❎"
                print(
                    f"\rWaiting for files: Linux {linux_tick} Windows {windows_tick} Mac {macos_tick}",
                    flush=True,
                    end="",
                )
                time.sleep(1)
        print()

    def next(self):
        ...


if __name__ == "__main__":
    repo = "/home/pablogsal/github/python/master"
    the_release = "3.10.1a2"
    automata = FiniteStateMachine(git_repo=repo, release=the_release)
    if not automata.load_checkpoint():
        automata.current_task = check_git
    automata.run()
