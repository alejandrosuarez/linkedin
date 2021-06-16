import requests
from bs4 import BeautifulSoup
from linkedin_api import Linkedin
from mautrix.bridge import custom_puppet as cpu
from mautrix.bridge.commands import command_handler, HelpSection
from mautrix.client import Client
from mautrix.errors import MForbidden

from .typehint import CommandEvent
from .. import puppet as pu

SECTION_AUTH = HelpSection("Authentication", 10, "")

missing_email = "Please use `$cmdprefix+sp login <email>` to log in here."
send_password = "Please send your password here to log in."
send_2fa_code = "Please send the PIN in your inbox here to complete login."

# LinkedIn Login URLs
SEED_URL = "https://www.linkedin.com/uas/login"
LOGIN_URL = "https://www.linkedin.com/checkpoint/lg/login-submit"
VERIFY_URL = "https://www.linkedin.com/checkpoint/challenge/verify"


@command_handler(
    needs_auth=False,
    management_only=False,
    help_section=SECTION_AUTH,
    help_text="See authentication status",
)
async def whoami(evt: CommandEvent):
    if not evt.sender.cookies:
        await evt.reply("You are not logged in")
    else:
        linkedin = Linkedin("", "", cookies=evt.sender.cookies)
        user_profile = linkedin.get_user_profile()
        first = user_profile.get("miniProfile", {}).get("firstName")
        last = user_profile.get("miniProfile", {}).get("lastName")
        await evt.reply(f"You are logged in as {first} {last}")


# region Login


@command_handler(
    needs_auth=False,
    management_only=False,
    help_section=SECTION_AUTH,
    help_text="Log in to LinkedIn",
    help_args="[_email_]",
)
async def login(evt: CommandEvent):
    if evt.sender.cookies:
        await evt.reply("You're already logged in.")
        return

    email = evt.args[0] if len(evt.args) > 0 else None

    if email:
        evt.sender.command_status = {
            "action": "Login",
            "room_id": evt.room_id,
            "next": enter_password,
            "email": email,
        }
        await evt.reply(send_password)
    else:
        await evt.reply(missing_email)


async def enter_password(evt: CommandEvent) -> None:
    try:
        await evt.az.intent.redact(evt.room_id, evt.event_id)
    except MForbidden:
        pass

    email = evt.sender.command_status["email"]
    password = evt.content.body

    # Try to log on
    session = requests.Session()
    text = session.get(SEED_URL).text
    soup = BeautifulSoup(text, "html.parser")
    login_csrf_param = soup.find("input", {"name": "loginCsrfParam"})["value"]
    payload = {
        "session_key": email,
        "loginCsrfParam": login_csrf_param,
        "session_password": password,
    }

    r = session.post(LOGIN_URL, data=payload)
    soup = BeautifulSoup(r.text, "html.parser")

    if (
        "liap" in session.cookies
        and "li_at" in session.cookies
        and "JSESSIONID" in session.cookies
    ):
        # No 2FA necessary.
        await evt.sender.on_logged_in(session.cookies)
        await evt.reply("Successfully logged in")
        return

    # TODO better detection of 2FA vs bad password
    if soup.find("input", {"name": "challengeId"}):
        payload = {
            k: soup.find("input", {"name": k})["value"]
            for k in (
                "csrfToken",
                "pageInstance",
                "resendUrl",
                "challengeId",
                "displayTime",
                "challengeSource",
                "requestSubmissionId",
                "challengeType",
                "challengeData",
                "challengeDetails",
                "failureRedirectUri",
            )
        }
        payload["language"] = ("en-US",)

        evt.sender.command_status = {
            "action": "Login",
            "room_id": evt.room_id,
            "next": enter_2fa_code,
            "payload": payload,
            "session": session,
            "email": email,
        }
        await evt.reply(
            "You have two-factor authentication turned on. Please enter the code you "
            "received via SMS or your authenticator app here."
        )
    else:
        evt.sender.command_status = None
        await evt.reply("Failed to log in")


async def enter_2fa_code(evt: CommandEvent) -> None:
    assert evt.sender.command_status, "something went terribly wrong"

    try:
        payload = evt.sender.command_status["payload"]
        payload["pin"] = "".join(evt.args).strip()

        session = evt.sender.command_status["session"]
        r = session.post(VERIFY_URL, data=payload)
        soup = BeautifulSoup(r.text, "html.parser")
        # print(soup)

        if (
            "liap" in session.cookies
            and "li_at" in session.cookies
            and "JSESSIONID" in session.cookies
        ):
            await evt.sender.on_logged_in(session.cookies)
            await evt.reply("Successfully logged in")
            evt.sender.command_status = None
            return

        # TODO actual error handling
        evt.sender.command_status = None
        await evt.reply("Failed to log in")

    except Exception as e:
        evt.log.exception("Failed to log in")
        evt.sender.command_status = None
        await evt.reply(f"Failed to log in: {e}")


# endregion

# region Matrix Puppeting


@command_handler(
    needs_auth=True,
    management_only=True,
    help_args="<_access token_>",
    help_section=SECTION_AUTH,
    help_text="Replace your Facebook Messenger account's "
    "Matrix puppet with your Matrix account",
)
async def login_matrix(evt: CommandEvent) -> None:
    puppet = await pu.Puppet.get_by_li_member_urn(evt.sender.li_member_urn)
    _, homeserver = Client.parse_mxid(evt.sender.mxid)
    if homeserver != pu.Puppet.hs_domain:
        await evt.reply("You can't log in with an account on a different homeserver")
        return
    try:
        await puppet.switch_mxid(" ".join(evt.args), evt.sender.mxid)
        await evt.reply(
            "Successfully replaced your Facebook Messenger account's "
            "Matrix puppet with your Matrix account."
        )
    except cpu.OnlyLoginSelf:
        await evt.reply("You may only log in with your own Matrix account")
    except cpu.InvalidAccessToken:
        await evt.reply("Invalid access token")


@command_handler(
    needs_auth=True,
    management_only=True,
    help_section=SECTION_AUTH,
    help_text="Revert your Facebook Messenger account's Matrix puppet to the original",
)
async def logout_matrix(evt: CommandEvent) -> None:
    puppet = await pu.Puppet.get_by_li_member_urn(evt.sender.li_member_urn)
    if not puppet.is_real_user:
        await evt.reply("You're not logged in with your Matrix account")
        return
    await puppet.switch_mxid(None, None)
    await evt.reply("Restored the original puppet for your Facebook Messenger account")


# endregion