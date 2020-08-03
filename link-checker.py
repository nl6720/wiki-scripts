#! /usr/bin/env python3

# FIXME: space-initialized code blocks should be skipped, but mwparserfromhell does not support that
# TODO: changes rejected interactively should be logged

import re
import logging

import requests
import mwparserfromhell

from ws.client import API, APIError
from ws.db.database import Database
from ws.utils import LazyProperty
from ws.interactive import edit_interactive, require_login, InteractiveQuit
from ws.diff import diff_highlighted
import ws.ArchWiki.lang as lang
from ws.parser_helpers.title import canonicalize, InvalidTitleCharError
from ws.checkers import get_edit_summary_tracker, CheckerBase, ManTemplateChecker, WikilinkChecker

logger = logging.getLogger(__name__)


class ExtlinkRules(CheckerBase):

    retype = type(re.compile(""))
    # list of (url_regex, text_cond, text_cond_flags, replacement) tuples, where:
    #   - url_regex: a regular expression matching the URL (using re.fullmatch)
    #   - text_cond:
    #       - as str: a format string used to create the regular expression described above
    #                 (it is formatted using the groups matched by url_regex)
    #       - as None: the extlink must not have any alternative text
    #   - text_cond_flags: flags for the text_cond regex
    #   - replacement: a format string used as a replacement (it is formatted
    #                  using the groups matched by url_regex and the alternative
    #                  text (if present))
    replacements = [
        # Arch bug tracker
        (re.escape("https://bugs.archlinux.org/task/") + "(\d+)",
            "(FS|flyspray) *#?{0}", 0, "{{{{Bug|{0}}}}}"),

        # official packages, with and without alternative text
        (r"https?\:\/\/(?:www\.)?archlinux\.org\/packages\/[\w-]+\/(?:any|i686|x86_64)\/([a-zA-Z0-9@._+-]+)\/?",
            "{0}", re.IGNORECASE, "{{{{Pkg|{0}}}}}"),
        (r"https?\:\/\/(?:www\.)?archlinux\.org\/packages\/[\w-]+\/(?:any|i686|x86_64)\/([a-zA-Z0-9@._+-]+)\/?",
            None, 0, "{{{{Pkg|{0}}}}}"),

        # AUR packages, with and without alternative text
        (r"https?\:\/\/aur\.archlinux\.org\/packages\/([a-zA-Z0-9@._+-]+)\/?",
            "{0}", re.IGNORECASE, "{{{{AUR|{0}}}}}"),
        (r"https?\:\/\/aur\.archlinux\.org\/packages\/([a-zA-Z0-9@._+-]+)\/?",
            None, 0, "{{{{AUR|{0}}}}}"),

        # Wikipedia interwiki
        (r"https?\:\/\/en\.wikipedia\.org\/wiki\/([^\]\?]+)",
            ".*", 0, "[[wikipedia:{0}|{1}]]"),
        (r"https?\:\/\/en\.wikipedia\.org\/wiki\/([^\]\?]+)",
            None, 0, "[[wikipedia:{0}]]"),

        # change http:// to https:// for archlinux.org and wikipedia.org (do it at the bottom, i.e. with least priority)
        (r"http:\/\/((?:[a-z]+\.)?(?:archlinux|wikipedia)\.org(?:\/\S+)?\/?)",
            ".*", 0, "[https://{0} {1}]"),
        (r"http:\/\/((?:[a-z]+\.)?(?:archlinux|wikipedia)\.org(?:\/\S+)?\/?)",
            None, 0, "https://{0}"),
    ]

    def __init__(self, api, db, **kwargs):
        super().__init__(api, db)

        _replacements = []
        for url_regex, text_cond, text_cond_flags, replacement in self.replacements:
            compiled = re.compile(url_regex)
            _replacements.append( (compiled, text_cond, text_cond_flags, replacement) )
        self.replacements = _replacements

    @LazyProperty
    def extlink_regex(self):
        general = self.api.site.general
        regex = re.escape(general["server"] + general["articlepath"].split("$1")[0])
        regex += "(?P<pagename>[^\s\?]+)"
        return re.compile(regex)

    @staticmethod
    def strip_extra_brackets(wikicode, extlink):
        """
        Strip extra brackets around an external link, for example:

            [[http://example.com/ foo]] -> [http://example.com/ foo]
        """
        parent, _ = wikicode._do_strong_search(extlink, True)
        index = parent.index(extlink)

        def _get_text(index):
            try:
                node = parent.get(index)
                if not isinstance(node, mwparserfromhell.nodes.text.Text):
                    return None
                return node
            except IndexError:
                return None

        prev = _get_text(index - 1)
        next_ = _get_text(index + 1)

        if prev is not None and next_ is not None and prev.endswith("[") and next_.startswith("]"):
            prev.value = prev.value[:-1]
            next_.value = next_.value[1:]

    def extlink_to_wikilink(self, wikicode, extlink):
        match = self.extlink_regex.fullmatch(str(extlink.url))
        if match:
            pagename = match.group("pagename")
            title = self.api.Title(pagename)
            target = title.format(iwprefix=True, namespace=True, sectionname=True)
            # handle links to special namespaces correctly
            if title.namespacenumber in {-2, 6, 14}:
                target = ":" + target
            if extlink.title:
                wikilink = "[[{}|{}]]".format(target, extlink.title)
            else:
                wikilink = "[[{}]]".format(target)
            wikicode.replace(extlink, wikilink)
            return True
        return False

    def extlink_replacements(self, wikicode, extlink):
        for url_regex, text_cond, text_cond_flags, replacement in self.replacements:
            if (text_cond is None and extlink.title is not None) or (text_cond is not None and extlink.title is None):
                continue
            match = url_regex.fullmatch(str(extlink.url))
            if match:
                if extlink.title is None:
                    repl = replacement.format(*match.groups())
                    # FIXME: hack to preserve brackets (e.g. [http://example.com/] )
                    if extlink.brackets and not repl.startswith("[") and not repl.endswith("]"):
                        repl = "[{}]".format(repl)
                    wikicode.replace(extlink, repl)
                    return True
                else:
                    groups = [re.escape(g) for g in match.groups()]
                    alt_text = str(extlink.title).strip()
                    if re.fullmatch(text_cond.format(*groups), alt_text, text_cond_flags):
                        wikicode.replace(extlink, replacement.format(*match.groups(), extlink.title))
                        return True
                    else:
                        logger.warning("external link that should be replaced, but has custom alternative text: {}".format(extlink))
        return False

    def update_extlink(self, wikicode, extlink):
        # always make sure to return as soon as the extlink is invalidated
        self.strip_extra_brackets(wikicode, extlink)
        if self.extlink_to_wikilink(wikicode, extlink):
            return
        if self.extlink_replacements(wikicode, extlink):
            return


class LinkChecker(ExtlinkRules, WikilinkChecker, ManTemplateChecker):

    interactive_only_pages = ["ArchWiki:Sandbox"]
    skip_pages = ["Table of contents", "Help:Editing", "ArchWiki:Reports", "ArchWiki:Requests", "ArchWiki:Statistics"]
    # article status templates, lowercase
    skip_templates = ["accuracy", "archive", "bad translation", "expansion", "laptop style", "merge", "move", "out of date", "remove", "stub", "style", "translateme"]

    def __init__(self, api, db, interactive=False, dry_run=False, first=None, title=None, langnames=None, connection_timeout=30, max_retries=3):
        if not dry_run:
            # ensure that we are authenticated
            require_login(api)

        # init base classes (pass keyword arguments for all base classes together)
        super().__init__(api, db, interactive=interactive, timeout=connection_timeout, max_retries=max_retries)

        self.dry_run = dry_run

        # parameters for self.run()
        self.first = first
        self.title = title
        self.langnames = langnames

        self.db.sync_with_api(api)
        self.db.sync_revisions_content(api, mode="latest")
        self.db.update_parser_cache()

    @staticmethod
    def set_argparser(argparser):
        # first try to set options for objects we depend on
        present_groups = [group.title for group in argparser._action_groups]
        if "Connection parameters" not in present_groups:
            API.set_argparser(argparser)
        if "Database parameters" not in present_groups:
            Database.set_argparser(argparser)

        group = argparser.add_argument_group(title="script parameters")
        group.add_argument("-i", "--interactive", action="store_true",
                help="enables interactive mode")
        group.add_argument("--dry-run", action="store_true",
                help="enables dry-run mode (changes are only shown and discarded)")
        mode = group.add_mutually_exclusive_group()
        mode.add_argument("--first", default=None, metavar="TITLE",
                help="the title of the first page to be processed")
        mode.add_argument("--title",
                help="the title of the only page to be processed")
        group.add_argument("--lang", default=None,
                help="comma-separated list of language tags to process (default: all, choices: {})".format(lang.get_internal_tags()))

    @classmethod
    def from_argparser(klass, args, api=None, db=None):
        if api is None:
            api = API.from_argparser(args)
        if db is None:
            db = Database.from_argparser(args)
        if args.lang:
            tags = args.lang.split(",")
            for tag in tags:
                if tag not in lang.get_internal_tags():
                    # FIXME: more elegant solution
                    raise Exception("{} is not a valid language tag".format(tag))
            langnames = {lang.langname_for_tag(tag) for tag in tags}
        else:
            langnames = set()
        return klass(api, db, interactive=args.interactive, dry_run=args.dry_run, first=args.first, title=args.title, langnames=langnames, connection_timeout=args.connection_timeout, max_retries=args.connection_max_retries)

    def update_page(self, src_title, text):
        """
        Parse the content of the page and call various methods to update the links.

        :param str src_title: title of the page
        :param str text: content of the page
        :returns: a (text, edit_summary) tuple, where text is the updated content
            and edit_summary is the description of performed changes
        """
        if lang.detect_language(src_title)[0] in self.skip_pages:
            logger.info("Skipping blacklisted page [[{}]]".format(src_title))
            return text, ""
        if lang.detect_language(src_title)[0] in self.interactive_only_pages and self.interactive is False:
            logger.info("Skipping page [[{}]] which is blacklisted for non-interactive mode".format(src_title))
            return text, ""

        logger.info("Parsing page [[{}]] ...".format(src_title))
        # FIXME: skip_style_tags=True is a partial workaround for https://github.com/earwig/mwparserfromhell/issues/40
        wikicode = mwparserfromhell.parse(text, skip_style_tags=True)
        summary_parts = []

        summary = get_edit_summary_tracker(wikicode, summary_parts)

        for extlink in wikicode.ifilter_external_links(recursive=True):
            # skip links inside article status templates
            parent = wikicode.get(wikicode.index(extlink, recursive=True))
            if isinstance(parent, mwparserfromhell.nodes.template.Template) and parent.name.lower() in self.skip_templates:
                continue
            with summary("replaced external links"):
                self.update_extlink(wikicode, extlink)

        for wikilink in wikicode.ifilter_wikilinks(recursive=True):
            # skip links inside article status templates
            parent = wikicode.get(wikicode.index(wikilink, recursive=True))
            if isinstance(parent, mwparserfromhell.nodes.template.Template) and parent.name.lower() in self.skip_templates:
                continue
            try:
                self.update_wikilink(wikicode, wikilink, src_title, summary_parts)
            # this can happen, e.g. due to [[{{TALKPAGENAME}}]]
            except InvalidTitleCharError:
                pass

        for template in wikicode.ifilter_templates(recursive=True):
            # skip templates that may be added or removed
            if any(canonicalize(template.name).startswith(prefix) for prefix in {"Broken section link", "Dead link"}):
                continue
            # skip links inside article status templates
            parent = wikicode.get(wikicode.index(template, recursive=True))
            if isinstance(parent, mwparserfromhell.nodes.template.Template) and parent.name.lower() in self.skip_templates:
                continue
            _pure_template = lang.detect_language(str(template.name))[0]
            if _pure_template.lower() in {"related", "related2"}:
                target = template.get(1).value
                # temporarily convert the {{Related}} to wikilink to reuse the update code
                wl = mwparserfromhell.nodes.wikilink.Wikilink(target)
                wikicode.replace(template, wl)
                # update
                try:
                    self.update_wikilink(wikicode, wl, src_title, summary_parts)
                # this can happen, e.g. due to [[{{TALKPAGENAME}}]]
                except InvalidTitleCharError:
                    continue
                # replace back
                target.value = str(wl.title)
                wikicode.replace(wl, template)
            elif template.name.lower() == "man":
                with summary("updated man page links"):
                    self.update_man_template(wikicode, template, src_title)

        # deduplicate and keep order
        parts = set()
        parts_add = parts.add
        summary_parts = [part for part in summary_parts if not (part in parts or parts_add(part))]

        edit_summary = ", ".join(summary_parts)
        if self.interactive is True:
            edit_summary += " (interactive)"

        return str(wikicode), edit_summary

    def _edit(self, title, pageid, text_new, text_old, timestamp, edit_summary):
        if text_old != text_new:
            if self.dry_run:
                diff = diff_highlighted(text_old, text_new, title + ".old", title + ".new", timestamp, "<utcnow>")
                print(diff)
                print("Edit summary:  " + edit_summary)
                print("(edit discarded due to --dry-run)")
            else:
                try:
                    if self.interactive is False:
                        self.api.edit(title, pageid, text_new, timestamp, edit_summary, bot="")
                    else:
                        edit_interactive(self.api, title, pageid, text_old, text_new, timestamp, edit_summary, bot="")
                except APIError as e:
                    pass

    def process_page(self, title):
        result = self.api.call_api(action="query", prop="revisions", rvprop="content|timestamp", rvslots="main", titles=title)
        page = list(result["pages"].values())[0]
        timestamp = page["revisions"][0]["timestamp"]
        text_old = page["revisions"][0]["slots"]["main"]["*"]
        text_new, edit_summary = self.update_page(title, text_old)
        self._edit(title, page["pageid"], text_new, text_old, timestamp, edit_summary)

    def process_allpages(self, apfrom=None, langnames=None):
        namespaces = [0, 4, 14]
        if self.interactive is True:
            namespaces.append(12)

        # rewind to the right namespace (the API throws BadTitle error if the
        # namespace of apfrom does not match apnamespace)
        if apfrom is not None:
            _title = self.api.Title(apfrom)
            if _title.namespacenumber not in namespaces:
                logger.error("Valid namespaces for the --first option are {}.".format([self.api.site.namespaces[ns] for ns in namespaces]))
                return
            while namespaces[0] != _title.namespacenumber:
                del namespaces[0]
            # apfrom must be without namespace prefix
            apfrom = _title.pagename

        for ns in namespaces:
            for page in self.db.query(generator="allpages", gaplimit="max", gapfilterredir="nonredirects", gapnamespace=ns, gapfrom=apfrom,
                                      prop="latestrevisions", rvprop={"timestamp", "content"}):
                title = page["title"]
                if langnames and lang.detect_language(title)[1] not in langnames:
                    continue
                _title = self.api.Title(title)
                timestamp = page["revisions"][0]["timestamp"]
                text_old = page["revisions"][0]["*"]
                text_new, edit_summary = self.update_page(title, text_old)
                self._edit(title, page["pageid"], text_new, text_old, timestamp, edit_summary)
            # the apfrom parameter is valid only for the first namespace
            apfrom = ""

    def run(self):
        if self.title is not None:
            checker.process_page(self.title)
        else:
            checker.process_allpages(apfrom=self.first, langnames=self.langnames)


if __name__ == "__main__":
    import ws.config

    checker = ws.config.object_from_argparser(LinkChecker, description="Parse all pages on the wiki and try to fix/simplify/beautify links")

    try:
        checker.run()
    except (InteractiveQuit, KeyboardInterrupt):
        pass
