#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

""" Usage:
    make_intl_data.py langtags [ldmlSupplemental.dtd supplementalMetadata.xml likelySubtags.xml]
    make_intl_data.py tzdata
    make_intl_data.py currency
    make_intl_data.py unicode-ext


    Target "langtags":
    This script extracts information about mappings between deprecated and
    current Unicode BCP 47 locale identifiers from CLDR and converts it to
    JavaScript object definitions in LangTagMappingsGenerated.js. The
    definitions are used in Intl.js.


    Target "tzdata":
    This script computes which time zone informations are not up-to-date in ICU
    and provides the necessary mappings to workaround this problem.
    https://ssl.icu-project.org/trac/ticket/12044


    Target "currency":
    Generates the mapping from currency codes to decimal digits used for them.

    Target "unicode-ext":
    Generates the mapping from deprecated BCP 47 Unicode extension values to
    their preferred values.
"""

from __future__ import print_function
import contextlib
import os
import re
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing
from functools import partial, total_ordering
from itertools import chain, groupby, tee
from operator import attrgetter, itemgetter
from zipfile import ZipFile

if sys.version_info.major == 2:
    from itertools import ifilter as filter, ifilterfalse as filterfalse, imap as map
    from urllib2 import urlopen, Request as UrlRequest
    from urlparse import urlsplit, urlunsplit
else:
    from itertools import filterfalse
    from urllib.request import urlopen, Request as UrlRequest
    from urllib.parse import urlsplit, urlunsplit


def writeMappingHeader(println, description, source, url):
    if type(description) is not list:
        description = [description]
    for desc in description:
        println(u"// {0}".format(desc))
    println(u"// Derived from {0}.".format(source))
    println(u"// {0}".format(url))


def writeMappingsVar(println, mapping, name, description, source, url):
    """ Writes a variable definition with a mapping table.

        Writes the contents of dictionary |mapping| through the |println|
        function with the given variable name and a comment with description,
        fileDate, and URL.
    """
    println(u"")
    writeMappingHeader(println, description, source, url)
    println(u"var {0} = {{".format(name))
    for key in sorted(mapping):
        if not isinstance(mapping[key], dict):
            value = mapping[key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            else:
                value = '"{0}"'.format(value)
        else:
            preferred = mapping[key]["preferred"]
            prefix = mapping[key]["prefix"]
            if key != preferred:
                raise Exception(
                    "Expected '{0}' matches preferred locale '{1}'".format(key, preferred))
            value = '"{0}"'.format(prefix)
        println(u'    "{0}": {1},'.format(key, value))
    println(u"};")


def writeUpdateLocaleIdMappingsFunction(println,
                                        complex_language_mappings,
                                        complex_region_mappings,
                                        description, source, url):
    """ Writes a function definition that performs language tag mapping. """
    println(u"")
    writeMappingHeader(println, description, source, url)
    println(u"""\
/* eslint-disable complexity */
function updateLocaleIdMappings(tag) {
    assert(IsObject(tag), "tag is an object");

    // Replace deprecated language tags with their preferred values.
    var language = tag.language;
    if (hasOwn(language, languageMappings)) {
        tag.language = languageMappings[language];
    } else if (hasOwn(language, complexLanguageMappings)) {
        switch (language) {""")

    # Merge duplicate language entries.
    language_aliases = {}
    for (deprecated_language, (language, script, region)) in (
        sorted(complex_language_mappings.items(), key=itemgetter(0))
    ):
        key = (language, script, region)
        if key not in language_aliases:
            language_aliases[key] = []
        else:
            language_aliases[key].append(deprecated_language)

    for (deprecated_language, (language, script, region)) in (
        sorted(complex_language_mappings.items(), key=itemgetter(0))
    ):
        key = (language, script, region)
        if deprecated_language in language_aliases[key]:
            continue

        for lang in [deprecated_language] + language_aliases[key]:
            println(u"""
          case "{}":
            """.format(lang).rstrip().strip("\n"))

        println(u"""
            tag.language = "{}";
        """.format(language).rstrip().strip("\n"))
        if script is not None:
            println(u"""
            if (tag.script === undefined)
                tag.script = "{}";
            """.format(script).rstrip().strip("\n"))
        if region is not None:
            println(u"""
            if (tag.region === undefined)
                tag.region = "{}";
            """.format(region).rstrip().strip("\n"))
        println(u"""
            break;
        """.rstrip().strip("\n"))

    println(u"""
          default:
            assert(false, "language not handled: " + language);
        }
    }

    // No script replacements are currently present.

    // Replace deprecated subtags with their preferred values.
    var region = tag.region;
    if (region !== undefined) {
        if (hasOwn(region, regionMappings)) {
            tag.region = regionMappings[region];
        } else if (hasOwn(region, complexRegionMappings)) {
            switch (region) {""".lstrip("\n"))

    # |non_default_replacements| is a list and hence not hashable. Convert it
    # to a string to get a proper hashable value.
    def hash_key(default, non_default_replacements):
        return (default, str(sorted(str(v) for v in non_default_replacements)))

    # Merge duplicate region entries.
    region_aliases = {}
    for (deprecated_region, (default, non_default_replacements)) in (
        sorted(complex_region_mappings.items(), key=itemgetter(0))
    ):
        key = hash_key(default, non_default_replacements)
        if key not in region_aliases:
            region_aliases[key] = []
        else:
            region_aliases[key].append(deprecated_region)

    for (deprecated_region, (default, non_default_replacements)) in (
        sorted(complex_region_mappings.items(), key=itemgetter(0))
    ):
        key = hash_key(default, non_default_replacements)
        if deprecated_region in region_aliases[key]:
            continue

        for region in [deprecated_region] + region_aliases[key]:
            println(u"""
              case "{}":
            """.format(region).rstrip().strip("\n"))

        for (language, script, region) in sorted(non_default_replacements, key=itemgetter(0)):
            if script is None:
                println(u"""
                if (tag.language === "{}") {{
                """.format(language).rstrip().strip("\n"))
            else:
                println(u"""
                if (tag.language === "{}" && tag.script === "{}") {{
                """.format(language, script).rstrip().strip("\n"))
            println(u"""
                    tag.region = "{}";
                    break;
                }}
            """.format(region).rstrip().strip("\n"))

        println(u"""
                tag.region = "{}";
                break;
        """.format(default).rstrip().strip("\n"))

    println(u"""
              default:
                assert(false, "region not handled: " + region);
            }
        }

        // No variant replacements are currently present.
        // No extension replacements are currently present.
        // Private use sequences are left as is.

    }
}
/* eslint-enable complexity */
""".strip("\n"))


def writeGrandfatheredMappingsFunction(println,
                                       grandfathered_mappings,
                                       description, source, url):
    """ Writes a function definition that maps grandfathered language tags. """
    println(u"")
    writeMappingHeader(println, description, source, url)
    println(u"""\
function updateGrandfatheredMappings(tag) {
    assert(IsObject(tag), "tag is an object");

    // We're mapping regular grandfathered tags to non-grandfathered form here.
    // Other tags remain unchanged.
    //
    // regular       = "art-lojban"
    //               / "cel-gaulish"
    //               / "no-bok"
    //               / "no-nyn"
    //               / "zh-guoyu"
    //               / "zh-hakka"
    //               / "zh-min"
    //               / "zh-min-nan"
    //               / "zh-xiang"
    //
    // Therefore we can quickly exclude most tags by checking every
    // |unicode_locale_id| subcomponent for characteristics not shared by any of
    // the regular grandfathered (RG) tags:
    //
    //   * Real-world |unicode_language_subtag|s are all two or three letters,
    //     so don't waste time running a useless |language.length > 3| fast-path.
    //   * No RG tag has a "script"-looking component.
    //   * No RG tag has a "region"-looking component.
    //   * The RG tags that match |unicode_locale_id| (art-lojban, cel-gaulish,
    //     zh-guoyu, zh-hakka, zh-xiang) have exactly one "variant". (no-bok,
    //     no-nyn, zh-min, and zh-min-nan require BCP47's extlang subtag
    //     that |unicode_locale_id| doesn't support.)
    //   * No RG tag contains |extensions| or |pu_extensions|.
    if (tag.script !== undefined ||
        tag.region !== undefined ||
        tag.variants.length !== 1 ||
        tag.extensions.length !== 0 ||
        tag.privateuse !== undefined)
    {
        return;
    }""")

    # From Unicode BCP 47 locale identifier <https://unicode.org/reports/tr35/>.
    #
    # Doesn't allow any 'extensions' subtags.
    re_unicode_locale_id = re.compile(
        r"""
        ^
        # unicode_language_id = unicode_language_subtag
        #     unicode_language_subtag = alpha{2,3} | alpha{5,8}
        (?P<language>[a-z]{2,3}|[a-z]{5,8})

        # (sep unicode_script_subtag)?
        #     unicode_script_subtag = alpha{4}
        (?:-(?P<script>[a-z]{4}))?

        # (sep unicode_region_subtag)?
        #     unicode_region_subtag = (alpha{2} | digit{3})
        (?:-(?P<region>([a-z]{2}|[0-9]{3})))?

        # (sep unicode_variant_subtag)*
        #     unicode_variant_subtag = (alphanum{5,8} | digit alphanum{3})
        (?P<variants>(-([a-z0-9]{5,8}|[0-9][a-z0-9]{3}))+)?

        # pu_extensions?
        #     pu_extensions = sep [xX] (sep alphanum{1,8})+
        (?:-(?P<privateuse>x(-[a-z0-9]{1,8})+))?
        $
        """, re.IGNORECASE | re.VERBOSE)

    is_first = True

    for (tag, modern) in sorted(grandfathered_mappings.items(), key=itemgetter(0)):
        tag_match = re_unicode_locale_id.match(tag)
        assert tag_match is not None

        tag_language = tag_match.group("language")
        assert tag_match.group("script") is None, (
               "{} does not contain a script subtag".format(tag))
        assert tag_match.group("region") is None, (
               "{} does not contain a region subtag".format(tag))
        tag_variants = tag_match.group("variants")
        assert tag_variants is not None, (
               "{} contains a variant subtag".format(tag))
        assert tag_match.group("privateuse") is None, (
               "{} does not contain a privateuse subtag".format(tag))

        tag_variant = tag_variants[1:]
        assert "-" not in tag_variant, (
               "{} contains only a single variant".format(tag))

        modern_match = re_unicode_locale_id.match(modern)
        assert modern_match is not None

        modern_language = modern_match.group("language")
        modern_script = modern_match.group("script")
        modern_region = modern_match.group("region")
        modern_variants = modern_match.group("variants")
        modern_privateuse = modern_match.group("privateuse")

        println(u"""
    // {} -> {}
""".format(tag, modern).rstrip())

        println(u"""
    {}if (tag.language === "{}" && tag.variants[0] === "{}") {{
        """.format("" if is_first else "else ", tag_language, tag_variant).rstrip().strip("\n"))

        is_first = False

        println(u"""
        tag.language = "{}";
        """.format(modern_language).rstrip().strip("\n"))

        if modern_script is not None:
            println(u"""
        tag.script = "{}";
        """.format(modern_script).rstrip().strip("\n"))

        if modern_region is not None:
            println(u"""
        tag.region = "{}";
        """.format(modern_region).rstrip().strip("\n"))

        if modern_variants is not None:
            println(u"""
        tag.variants = {};
        """.format(sorted(modern_variants[1:].split("-"))).rstrip().strip("\n"))
        else:
            println(u"""
        tag.variants.length = 0;
        """.rstrip().strip("\n"))

        if modern_privateuse is not None:
            println(u"""
        tag.privateuse = "{}";
        """.format(modern_privateuse).rstrip().strip("\n"))

        println(u"""
    }""".rstrip().strip("\n"))

    println(u"""
}""".lstrip("\n"))


@contextlib.contextmanager
def TemporaryDirectory():
    tmpDir = tempfile.mkdtemp()
    try:
        yield tmpDir
    finally:
        shutil.rmtree(tmpDir)


def readSupplementalData(supplemental_dtd_file, supplemental_metadata_file, likely_subtags_file):
    """ Reads CLDR Supplemental Data and extracts information for Intl.js.

        Information extracted:
        - grandfatheredMappings: mappings from grandfathered tags to preferred
          complete language tags
        - languageMappings: mappings from language subtags to preferred subtags
        - complexLanguageMappings: mappings from language subtags with complex rules
        - regionMappings: mappings from region subtags to preferred subtags
        - complexRegionMappings: mappings from region subtags with complex rules
        - likelySubtags: likely subtags used for generating test data only
        Returns these mappings as dictionaries.
    """
    import xml.etree.ElementTree as ET

    # <!ATTLIST version cldrVersion CDATA #FIXED "36" >
    re_cldr_version = re.compile(
        r"""<!ATTLIST version cldrVersion CDATA #FIXED "(?P<version>[\d|\.]+)" >""")

    with io.open(supplemental_dtd_file, mode="r", encoding="utf-8") as f:
        version_match = re_cldr_version.search(f.read())
        assert version_match is not None, "CLDR version string not found"
        cldr_version = version_match.group("version")

    # From Unicode BCP 47 locale identifier <https://unicode.org/reports/tr35/>.
    re_unicode_language_id = re.compile(
        r"""
        ^
        # unicode_language_id = unicode_language_subtag
        #     unicode_language_subtag = alpha{2,3} | alpha{5,8}
        (?P<language>[a-z]{2,3}|[a-z]{5,8})

        # (sep unicode_script_subtag)?
        #     unicode_script_subtag = alpha{4}
        (?:-(?P<script>[a-z]{4}))?

        # (sep unicode_region_subtag)?
        #     unicode_region_subtag = (alpha{2} | digit{3})
        (?:-(?P<region>([a-z]{2}|[0-9]{3})))?

        # (sep unicode_variant_subtag)*
        #     unicode_variant_subtag = (alphanum{5,8} | digit alphanum{3})
        (?P<variants>(-([a-z0-9]{5,8}|[0-9][a-z0-9]{3}))+)?
        $
        """, re.IGNORECASE | re.VERBOSE)

    re_unicode_language_subtag = re.compile(
        r"""
        ^
        # unicode_language_subtag = alpha{2,3} | alpha{5,8}
        ([a-z]{2,3}|[a-z]{5,8})
        $
        """, re.IGNORECASE | re.VERBOSE)

    re_unicode_region_subtag = re.compile(
        r"""
        ^
        # unicode_region_subtag = (alpha{2} | digit{3})
        ([a-z]{2}|[0-9]{3})
        $
        """, re.IGNORECASE | re.VERBOSE)

    # The fixed list of BCP 47 grandfathered language tags.
    grandfathered_tags = (
        "art-lojban",
        "cel-gaulish",
        "en-GB-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "no-bok",
        "no-nyn",
        "sgn-BE-FR",
        "sgn-BE-NL",
        "sgn-CH-DE",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    )

    # The list of grandfathered tags which are valid Unicode BCP 47 locale identifiers.
    unicode_bcp47_grandfathered_tags = {tag for tag in grandfathered_tags
                                        if re_unicode_language_id.match(tag)}

    # Dictionary of simple language subtag mappings, e.g. "in" -> "id".
    language_mappings = {}

    # Dictionary of complex language subtag mappings, modifying more than one
    # subtag, e.g. "sh" -> ("sr", "Latn", None) and "cnr" -> ("sr", None, "ME").
    complex_language_mappings = {}

    # Dictionary of simple region subtag mappings, e.g. "DD" -> "DE".
    region_mappings = {}

    # Dictionary of complex region subtag mappings, containing more than one
    # replacement, e.g. "SU" -> ("RU", ["AM", "AZ", "BY", ...]).
    complex_region_mappings = {}

    # Dictionary of grandfathered mappings to preferred values.
    grandfathered_mappings = {}

    # CLDR uses "_" as the separator for some elements. Replace it with "-".
    def bcp47_id(cldr_id):
        return cldr_id.replace("_", "-")

    # CLDR uses the canonical case for most entries, but there are some
    # exceptions, like:
    #   <languageAlias type="drw" replacement="fa_af" reason="deprecated"/>
    # Therefore canonicalize all tags to be on the safe side.
    def bcp47_canonical(language, script, region):
        # Canonical case for language subtags is lower case.
        # Canonical case for script subtags is title case.
        # Canonical case for region subtags is upper case.
        return (language.lower() if language else None,
                script.title() if script else None,
                region.upper() if region else None)

    tree = ET.parse(supplemental_metadata_file)

    for language_alias in tree.iterfind(".//languageAlias"):
        type = bcp47_id(language_alias.get("type"))
        replacement = bcp47_id(language_alias.get("replacement"))

        # Handle grandfathered mappings first.
        if type in unicode_bcp47_grandfathered_tags:
            grandfathered_mappings[type] = replacement
            continue

        # We're only interested in language subtag matches, so ignore any
        # entries which have additional subtags.
        if re_unicode_language_subtag.match(type) is None:
            continue

        if re_unicode_language_subtag.match(replacement) is not None:
            # Canonical case for language subtags is lower-case.
            language_mappings[type] = replacement.lower()
        else:
            replacement_match = re_unicode_language_id.match(replacement)
            assert replacement_match is not None, (
                   "{} invalid Unicode BCP 47 locale identifier".format(replacement))
            assert replacement_match.group("variants") is None, (
                   "{}: unexpected variant subtags in {}".format(type, replacement))

            complex_language_mappings[type] = bcp47_canonical(replacement_match.group("language"),
                                                              replacement_match.group("script"),
                                                              replacement_match.group("region"))

    for territory_alias in tree.iterfind(".//territoryAlias"):
        type = territory_alias.get("type")
        replacement = territory_alias.get("replacement")

        # We're only interested in region subtag matches, so ignore any entries
        # which contain legacy formats, e.g. three letter region codes.
        if re_unicode_region_subtag.match(type) is None:
            continue

        if re_unicode_region_subtag.match(replacement) is not None:
            # Canonical case for region subtags is upper-case.
            region_mappings[type] = replacement.upper()
        else:
            # Canonical case for region subtags is upper-case.
            replacements = [r.upper() for r in replacement.split(" ")]
            assert all(
                re_unicode_region_subtag.match(loc) is not None for loc in replacements
            ), "{} invalid region subtags".format(replacement)
            complex_region_mappings[type] = replacements

    tree = ET.parse(likely_subtags_file)

    likely_subtags = {}

    for likely_subtag in tree.iterfind(".//likelySubtag"):
        from_tag = bcp47_id(likely_subtag.get("from"))
        from_match = re_unicode_language_id.match(from_tag)
        assert from_match is not None, (
               "{} invalid Unicode BCP 47 locale identifier".format(from_tag))
        assert from_match.group("variants") is None, (
               "unexpected variant subtags in {}".format(from_tag))

        to_tag = bcp47_id(likely_subtag.get("to"))
        to_match = re_unicode_language_id.match(to_tag)
        assert to_match is not None, (
               "{} invalid Unicode BCP 47 locale identifier".format(to_tag))
        assert to_match.group("variants") is None, (
               "unexpected variant subtags in {}".format(to_tag))

        from_canonical = bcp47_canonical(from_match.group("language"),
                                         from_match.group("script"),
                                         from_match.group("region"))

        to_canonical = bcp47_canonical(to_match.group("language"),
                                       to_match.group("script"),
                                       to_match.group("region"))

        likely_subtags[from_canonical] = to_canonical

    complex_region_mappings_final = {}

    for (deprecated_region, replacements) in complex_region_mappings.items():
        # Find all likely subtag entries which don't already contain a region
        # subtag and whose target region is in the list of replacement regions.
        region_likely_subtags = [(from_language, from_script, to_region)
                                 for ((from_language, from_script, from_region),
                                      (_, _, to_region)) in likely_subtags.items()
                                 if from_region is None and to_region in replacements]

        # The first replacement entry is the default region.
        default = replacements[0]

        # Find all likely subtag entries whose region matches the default region.
        default_replacements = {(language, script)
                                for (language, script, region) in region_likely_subtags
                                if region == default}

        # And finally find those entries which don't use the default region.
        # These are the entries we're actually interested in, because those need
        # to be handled specially when selecting the correct preferred region.
        non_default_replacements = [(language, script, region)
                                    for (language, script, region) in region_likely_subtags
                                    if (language, script) not in default_replacements]

        # If there are no non-default replacements, we can handle the region as
        # part of the simple region mapping.
        if non_default_replacements:
            complex_region_mappings_final[deprecated_region] = (default, non_default_replacements)
        else:
            region_mappings[deprecated_region] = default

    return {"version": cldr_version,
            "grandfatheredMappings": grandfathered_mappings,
            "languageMappings": language_mappings,
            "complexLanguageMappings": complex_language_mappings,
            "regionMappings": region_mappings,
            "complexRegionMappings": complex_region_mappings_final,
            "likelySubtags": likely_subtags,
            }


def writeCLDRLanguageTagData(println, data, url):
    """ Writes the language tag data to the Intl data file. """

    source = u"CLDR Supplemental Data, version {}".format(data["version"])
    grandfathered_mappings = data["grandfatheredMappings"]
    language_mappings = data["languageMappings"]
    complex_language_mappings = data["complexLanguageMappings"]
    region_mappings = data["regionMappings"]
    complex_region_mappings = data["complexRegionMappings"]

    writeMappingsVar(println, grandfathered_mappings, "grandfatheredMappings",
                     "Mappings from grandfathered tags to preferred values.", source, url)
    writeMappingsVar(println, language_mappings, "languageMappings",
                     "Mappings from language subtags to preferred values.", source, url)
    writeMappingsVar(println, {key: True for key in complex_language_mappings},
                     "complexLanguageMappings",
                     "Language subtags with complex mappings.", source, url)
    writeMappingsVar(println, region_mappings, "regionMappings",
                     "Mappings from region subtags to preferred values.", source, url)
    writeMappingsVar(println, {key: True for key in complex_region_mappings},
                     "complexRegionMappings",
                     "Region subtags with complex mappings.", source, url)

    writeUpdateLocaleIdMappingsFunction(println, complex_language_mappings,
                                        complex_region_mappings,
                                        "Canonicalize Unicode BCP 47 locale identifiers.",
                                        source, url)
    writeGrandfatheredMappingsFunction(println, grandfathered_mappings,
                                       "Canonicalize grandfathered locale identifiers.",
                                       source, url)


def writeCLDRLanguageTagLikelySubtagsTest(println, data, url):
    """ Writes the likely-subtags test file. """

    source = u"CLDR Supplemental Data, version {}".format(data["version"])
    language_mappings = data["languageMappings"]
    complex_language_mappings = data["complexLanguageMappings"]
    region_mappings = data["regionMappings"]
    complex_region_mappings = data["complexRegionMappings"]
    likely_subtags = data["likelySubtags"]

    def bcp47(tag):
        (language, script, region) = tag
        return "{}{}{}".format(language,
                               "-" + script if script else "",
                               "-" + region if region else "")

    def canonical(tag):
        (language, script, region) = tag

        # Map deprecated language subtags.
        if language in language_mappings:
            language = language_mappings[language]
        elif language in complex_language_mappings:
            (language2, script2, region2) = complex_language_mappings[language]
            (language, script, region) = (language2,
                                          script if script else script2,
                                          region if region else region2)

        # Map deprecated region subtags.
        if region in region_mappings:
            region = region_mappings[region]
        else:
            # Assume no complex region mappings are needed for now.
            assert region not in complex_region_mappings,\
                   "unexpected region with complex mappings: {}".format(region)

        return (language, script, region)

    # https://unicode.org/reports/tr35/#Likely_Subtags

    def addLikelySubtags(tag):
        # Step 1: Canonicalize.
        (language, script, region) = canonical(tag)
        if script == "Zzzz":
            script = None
        if region == "ZZ":
            region = None

        # Step 2: Lookup.
        searches = ((language, script, region),
                    (language, None, region),
                    (language, script, None),
                    (language, None, None),
                    ("und", script, None))
        search = next(search for search in searches if search in likely_subtags)

        (language_s, script_s, region_s) = search
        (language_m, script_m, region_m) = likely_subtags[search]

        # Step 3: Return.
        return (language if language != language_s else language_m,
                script if script != script_s else script_m,
                region if region != region_s else region_m)

    # https://unicode.org/reports/tr35/#Likely_Subtags
    def removeLikelySubtags(tag):
        # Step 1: Add likely subtags.
        max = addLikelySubtags(tag)

        # Step 2: Remove variants (doesn't apply here).

        # Step 3: Find a match.
        (language, script, region) = max
        for trial in ((language, None, None), (language, None, region), (language, script, None)):
            if addLikelySubtags(trial) == max:
                return trial

        # Step 4: Return maximized if no match found.
        return max

    def likely_canonical(from_tag, to_tag):
        # Canonicalize the input tag.
        from_tag = canonical(from_tag)

        # Update the expected result if necessary.
        if from_tag in likely_subtags:
            to_tag = likely_subtags[from_tag]

        # Canonicalize the expected output.
        to_canonical = canonical(to_tag)

        # Sanity check: This should match the result of |addLikelySubtags|.
        assert to_canonical == addLikelySubtags(from_tag)

        return to_canonical

    # |likely_subtags| contains non-canonicalized tags, so canonicalize it first.
    likely_subtags_canonical = {k: likely_canonical(k, v) for (k, v) in likely_subtags.items()}

    # Add test data for |Intl.Locale.prototype.maximize()|.
    writeMappingsVar(println, {bcp47(k): bcp47(v) for (k, v) in likely_subtags_canonical.items()},
                     "maxLikelySubtags", "Extracted from likelySubtags.xml.", source, url)

    # Use the maximalized tags as the input for the remove likely-subtags test.
    minimized = {tag: removeLikelySubtags(tag) for tag in likely_subtags_canonical.values()}

    # Add test data for |Intl.Locale.prototype.minimize()|.
    writeMappingsVar(println, {bcp47(k): bcp47(v) for (k, v) in minimized.items()},
                     "minLikelySubtags", "Extracted from likelySubtags.xml.", source, url)

    println(u"""
for (let [tag, maximal] of Object.entries(maxLikelySubtags)) {
    assertEq(new Intl.Locale(tag).maximize().toString(), maximal);
}""")

    println(u"""
for (let [tag, minimal] of Object.entries(minLikelySubtags)) {
    assertEq(new Intl.Locale(tag).minimize().toString(), minimal);
}""")

    println(u"""
if (typeof reportCompare === "function")
    reportCompare(0, 0);""")


def updateCLDRLangTags(args):
    """ Update the LangTagMappingsCLDRGenerated.js file. """
    url = args.url
    branch = args.branch
    revision = args.revision
    out = args.out
    files = args.files

    print("Arguments:")
    print("\tDownload url: %s" % url)
    print("\tBranch: %s" % branch)
    print("\tRevision: %s" % revision)
    print("\tLocal supplemental data and likely subtags: %s" % files)
    print("\tOutput file: %s" % out)
    print("")

    if files:
        if len(files) != 3:
            raise Exception("Expected three files, but got: {}".format(files))

        print(("Always make sure you have the newest ldmlSupplemental.dtd, "
               "supplementalMetadata.xml, and likelySubtags.xml!"))

        supplemental_dtd_file = files[0]
        supplemental_metadata_file = files[1]
        likely_subtags_file = files[2]
    else:
        print("Downloading CLDR supplemental data...")

        supplemental_dtd_filename = "ldmlSupplemental.dtd"
        supplemental_dtd_path = "common/dtd/{}".format(supplemental_dtd_filename)
        supplemental_dtd_file = os.path.join(os.getcwd(), supplemental_dtd_filename)

        supplemental_metadata_filename = "supplementalMetadata.xml"
        supplemental_metadata_path = "common/supplemental/{}".format(
            supplemental_metadata_filename)
        supplemental_metadata_file = os.path.join(os.getcwd(), supplemental_metadata_filename)

        likely_subtags_filename = "likelySubtags.xml"
        likely_subtags_path = "common/supplemental/{}".format(likely_subtags_filename)
        likely_subtags_file = os.path.join(os.getcwd(), likely_subtags_filename)

        # Try to download the raw file directly from GitHub if possible.
        split = urlsplit(url)
        if split.netloc == "github.com" and split.path.endswith(".git") and revision == "HEAD":
            def download(path, file):
                urlpath = "{}/raw/{}/{}".format(urlsplit(url).path[:-4], branch, path)
                raw_url = urlunsplit((split.scheme, split.netloc, urlpath, split.query,
                                      split.fragment))

                with closing(urlopen(raw_url)) as reader:
                    text = reader.read().decode("utf-8")
                with io.open(file, "w", encoding="utf-8") as saved_file:
                    saved_file.write(text)

            download(supplemental_dtd_path, supplemental_dtd_file)
            download(supplemental_metadata_path, supplemental_metadata_file)
            download(likely_subtags_path, likely_subtags_file)
        else:
            # Download the requested branch in a temporary directory.
            with TemporaryDirectory() as inDir:
                if revision == "HEAD":
                    subprocess.check_call(["git", "clone", "--depth=1",
                                           "--branch=%s" % branch, url, inDir])
                else:
                    subprocess.check_call(["git", "clone", "--single-branch",
                                           "--branch=%s" % branch, url, inDir])
                    subprocess.check_call(["git", "-C", inDir, "reset", "--hard", revision])

                    shutil.copyfile(os.path.join(inDir, supplemental_dtd_path),
                                    supplemental_dtd_file)
                    shutil.copyfile(os.path.join(inDir, supplemental_metadata_path),
                                    supplemental_metadata_file)
                    shutil.copyfile(os.path.join(inDir, likely_subtags_path), likely_subtags_file)

    print("Processing CLDR supplemental data...")
    data = readSupplementalData(supplemental_dtd_file,
                                supplemental_metadata_file,
                                likely_subtags_file)

    print("Writing Intl data...")
    with io.open(out, mode="w", encoding="utf-8", newline="") as f:
        println = partial(print, file=f)

        println(u"// Generated by make_intl_data.py. DO NOT EDIT.")
        writeCLDRLanguageTagData(println, data, url)

    print("Writing Intl test data...")
    test_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "../../tests/non262/Intl/Locale/likely-subtags-generated.js")
    with io.open(test_file, mode="w", encoding="utf-8", newline="") as f:
        println = partial(print, file=f)

        println(u"// |reftest| skip-if(!this.hasOwnProperty('Intl')||"
                u"(!this.Intl.Locale&&!this.hasOwnProperty('addIntlExtras')))")
        println(u"// Generated by make_intl_data.py. DO NOT EDIT.")
        writeCLDRLanguageTagLikelySubtagsTest(println, data, url)


def flines(filepath, encoding="utf-8"):
    """ Open filepath and iterate over its content. """
    with io.open(filepath, mode="r", encoding=encoding) as f:
        for line in f:
            yield line


@total_ordering
class Zone(object):
    """ Time zone with optional file name. """

    def __init__(self, name, filename=""):
        self.name = name
        self.filename = filename

    def __eq__(self, other):
        return hasattr(other, "name") and self.name == other.name

    def __lt__(self, other):
        return self.name < other.name

    def __hash__(self):
        return hash(self.name)

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name


class TzDataDir(object):
    """ tzdata source from a directory. """

    def __init__(self, obj):
        self.name = partial(os.path.basename, obj)
        self.resolve = partial(os.path.join, obj)
        self.basename = os.path.basename
        self.isfile = os.path.isfile
        self.listdir = partial(os.listdir, obj)
        self.readlines = flines


class TzDataFile(object):
    """ tzdata source from a file (tar or gzipped). """

    def __init__(self, obj):
        self.name = lambda: os.path.splitext(os.path.splitext(os.path.basename(obj))[0])[0]
        self.resolve = obj.getmember
        self.basename = attrgetter("name")
        self.isfile = tarfile.TarInfo.isfile
        self.listdir = obj.getnames
        self.readlines = partial(self._tarlines, obj)

    def _tarlines(self, tar, m):
        with closing(tar.extractfile(m)) as f:
            for line in f:
                yield line.decode("utf-8")


def validateTimeZones(zones, links):
    """ Validate the zone and link entries. """
    linkZones = set(links.keys())
    intersect = linkZones.intersection(zones)
    if intersect:
        raise RuntimeError("Links also present in zones: %s" % intersect)

    zoneNames = {z.name for z in zones}
    linkTargets = set(links.values())
    if not linkTargets.issubset(zoneNames):
        raise RuntimeError("Link targets not found: %s" % linkTargets.difference(zoneNames))


def partition(iterable, *predicates):
    def innerPartition(pred, it):
        it1, it2 = tee(it)
        return (filter(pred, it1), filterfalse(pred, it2))
    if len(predicates) == 0:
        return iterable
    (left, right) = innerPartition(predicates[0], iterable)
    if len(predicates) == 1:
        return (left, right)
    return tuple([left] + list(partition(right, *predicates[1:])))


def listIANAFiles(tzdataDir):
    def isTzFile(d, m, f):
        return m(f) and d.isfile(d.resolve(f))
    return filter(partial(isTzFile, tzdataDir, re.compile("^[a-z0-9]+$").match),
                  tzdataDir.listdir())


def readIANAFiles(tzdataDir, files):
    """ Read all IANA time zone files from the given iterable. """
    nameSyntax = "[\w/+\-]+"
    pZone = re.compile(r"Zone\s+(?P<name>%s)\s+.*" % nameSyntax)
    pLink = re.compile(r"Link\s+(?P<target>%s)\s+(?P<name>%s)(?:\s+#.*)?" %
                       (nameSyntax, nameSyntax))

    def createZone(line, fname):
        match = pZone.match(line)
        name = match.group("name")
        return Zone(name, fname)

    def createLink(line, fname):
        match = pLink.match(line)
        (name, target) = match.group("name", "target")
        return (Zone(name, fname), target)

    zones = set()
    links = dict()
    for filename in files:
        filepath = tzdataDir.resolve(filename)
        for line in tzdataDir.readlines(filepath):
            if line.startswith("Zone"):
                zones.add(createZone(line, filename))
            if line.startswith("Link"):
                (link, target) = createLink(line, filename)
                links[link] = target

    return (zones, links)


def readIANATimeZones(tzdataDir, ignoreBackzone, ignoreFactory):
    """ Read the IANA time zone information from `tzdataDir`. """

    backzoneFiles = {"backzone"}
    (bkfiles, tzfiles) = partition(listIANAFiles(tzdataDir), backzoneFiles.__contains__)

    # Read zone and link infos.
    (zones, links) = readIANAFiles(tzdataDir, tzfiles)
    (backzones, backlinks) = readIANAFiles(tzdataDir, bkfiles)

    # Remove the placeholder time zone "Factory".
    if ignoreFactory:
        zones.remove(Zone("Factory"))

    # Merge with backzone data.
    if not ignoreBackzone:
        zones |= backzones
        links = {name: target for name, target in links.items() if name not in backzones}
        links.update(backlinks)

    validateTimeZones(zones, links)

    return (zones, links)


def readICUResourceFile(filename):
    """ Read an ICU resource file.

        Yields (<table-name>, <startOrEnd>, <value>) for each table.
    """

    numberValue = r"-?\d+"
    stringValue = r'".+?"'

    def asVector(val): return r"%s(?:\s*,\s*%s)*" % (val, val)
    numberVector = asVector(numberValue)
    stringVector = asVector(stringValue)

    reNumberVector = re.compile(numberVector)
    reStringVector = re.compile(stringVector)
    reNumberValue = re.compile(numberValue)
    reStringValue = re.compile(stringValue)

    def parseValue(value):
        m = reNumberVector.match(value)
        if m:
            return [int(v) for v in reNumberValue.findall(value)]
        m = reStringVector.match(value)
        if m:
            return [v[1:-1] for v in reStringValue.findall(value)]
        raise RuntimeError("unknown value type: %s" % value)

    def extractValue(values):
        if len(values) == 0:
            return None
        if len(values) == 1:
            return values[0]
        return values

    def line(*args):
        maybeMultiComments = r"(?:/\*[^*]*\*/)*"
        maybeSingleComment = r"(?://.*)?"
        lineStart = "^%s" % maybeMultiComments
        lineEnd = "%s\s*%s$" % (maybeMultiComments, maybeSingleComment)
        return re.compile(r"\s*".join(chain([lineStart], args, [lineEnd])))

    tableName = r'(?P<quote>"?)(?P<name>.+?)(?P=quote)'
    tableValue = r"(?P<value>%s|%s)" % (numberVector, stringVector)

    reStartTable = line(tableName, r"\{")
    reEndTable = line(r"\}")
    reSingleValue = line(r",?", tableValue, r",?")
    reCompactTable = line(tableName, r"\{", tableValue, r"\}")
    reEmptyLine = line()

    tables = []

    def currentTable(): return "|".join(tables)
    values = []
    for line in flines(filename, "utf-8-sig"):
        line = line.strip()
        if line == "":
            continue

        m = reEmptyLine.match(line)
        if m:
            continue

        m = reStartTable.match(line)
        if m:
            assert len(values) == 0
            tables.append(m.group("name"))
            continue

        m = reEndTable.match(line)
        if m:
            yield (currentTable(), extractValue(values))
            tables.pop()
            values = []
            continue

        m = reCompactTable.match(line)
        if m:
            assert len(values) == 0
            tables.append(m.group("name"))
            yield (currentTable(), extractValue(parseValue(m.group("value"))))
            tables.pop()
            continue

        m = reSingleValue.match(line)
        if m and tables:
            values.extend(parseValue(m.group("value")))
            continue

        raise RuntimeError("unknown entry: %s" % line)


def readICUTimeZonesFromTimezoneTypes(icuTzDir):
    """ Read the ICU time zone information from `icuTzDir`/timezoneTypes.txt
        and returns the tuple (zones, links).
    """
    typeMapTimeZoneKey = "timezoneTypes:table(nofallback)|typeMap|timezone|"
    typeAliasTimeZoneKey = "timezoneTypes:table(nofallback)|typeAlias|timezone|"

    def toTimeZone(name): return Zone(name.replace(":", "/"))

    zones = set()
    links = dict()

    for name, value in readICUResourceFile(os.path.join(icuTzDir, "timezoneTypes.txt")):
        if name.startswith(typeMapTimeZoneKey):
            zones.add(toTimeZone(name[len(typeMapTimeZoneKey):]))
        if name.startswith(typeAliasTimeZoneKey):
            links[toTimeZone(name[len(typeAliasTimeZoneKey):])] = value

    # Remove the ICU placeholder time zone "Etc/Unknown".
    zones.remove(Zone("Etc/Unknown"))

    # tzdata2017c removed the link Canada/East-Saskatchewan -> America/Regina,
    # but it is still present in ICU sources. Manually remove it to keep our
    # tables consistent with IANA.
    del links[Zone("Canada/East-Saskatchewan")]

    validateTimeZones(zones, links)

    return (zones, links)


def readICUTimeZonesFromZoneInfo(icuTzDir, ignoreFactory):
    """ Read the ICU time zone information from `icuTzDir`/zoneinfo64.txt
        and returns the tuple (zones, links).
    """
    zoneKey = "zoneinfo64:table(nofallback)|Zones:array|:table"
    linkKey = "zoneinfo64:table(nofallback)|Zones:array|:int"
    namesKey = "zoneinfo64:table(nofallback)|Names"

    tzId = 0
    tzLinks = dict()
    tzNames = []

    for name, value in readICUResourceFile(os.path.join(icuTzDir, "zoneinfo64.txt")):
        if name == zoneKey:
            tzId += 1
        elif name == linkKey:
            tzLinks[tzId] = int(value)
            tzId += 1
        elif name == namesKey:
            tzNames.extend(value)

    links = {Zone(tzNames[zone]): tzNames[target] for (zone, target) in tzLinks.items()}
    zones = {Zone(v) for v in tzNames if Zone(v) not in links}

    # Remove the ICU placeholder time zone "Etc/Unknown".
    zones.remove(Zone("Etc/Unknown"))

    # tzdata2017c removed the link Canada/East-Saskatchewan -> America/Regina,
    # but it is still present in ICU sources. Manually remove it to keep our
    # tables consistent with IANA.
    del links[Zone("Canada/East-Saskatchewan")]

    # Remove the placeholder time zone "Factory".
    if ignoreFactory:
        zones.remove(Zone("Factory"))

    validateTimeZones(zones, links)

    return (zones, links)


def readICUTimeZones(icuDir, icuTzDir, ignoreFactory):
    # zoneinfo64.txt contains the supported time zones by ICU. This data is
    # generated from tzdata files, it doesn't include "backzone" in stock ICU.
    (zoneinfoZones, zoneinfoLinks) = readICUTimeZonesFromZoneInfo(icuTzDir, ignoreFactory)

    # timezoneTypes.txt contains the canonicalization information for ICU. This
    # data is generated from CLDR files. It includes data about time zones from
    # tzdata's "backzone" file.
    (typesZones, typesLinks) = readICUTimeZonesFromTimezoneTypes(icuTzDir)

    # Information in zoneinfo64 should be a superset of timezoneTypes.
    def inZoneInfo64(zone): return zone in zoneinfoZones or zone in zoneinfoLinks

    # Remove legacy ICU time zones from zoneinfo64 data.
    (legacyZones, legacyLinks) = readICULegacyZones(icuDir)
    zoneinfoZones = {zone for zone in zoneinfoZones if zone not in legacyZones}
    zoneinfoLinks = {zone: target for (zone, target) in zoneinfoLinks.items()
                     if zone not in legacyLinks}

    notFoundInZoneInfo64 = [zone for zone in typesZones if not inZoneInfo64(zone)]
    if notFoundInZoneInfo64:
        raise RuntimeError("Missing time zones in zoneinfo64.txt: %s" % notFoundInZoneInfo64)

    notFoundInZoneInfo64 = [zone for zone in typesLinks.keys() if not inZoneInfo64(zone)]
    if notFoundInZoneInfo64:
        raise RuntimeError("Missing time zones in zoneinfo64.txt: %s" % notFoundInZoneInfo64)

    # zoneinfo64.txt only defines the supported time zones by ICU, the canonicalization
    # rules are defined through timezoneTypes.txt. Merge both to get the actual zones
    # and links used by ICU.
    icuZones = set(chain(
        (zone for zone in zoneinfoZones if zone not in typesLinks),
        (zone for zone in typesZones)
    ))
    icuLinks = dict(chain(
        ((zone, target) for (zone, target) in zoneinfoLinks.items() if zone not in typesZones),
        ((zone, target) for (zone, target) in typesLinks.items())
    ))

    return (icuZones, icuLinks)


def readICULegacyZones(icuDir):
    """ Read the ICU legacy time zones from `icuTzDir`/tools/tzcode/icuzones
        and returns the tuple (zones, links).
    """
    tzdir = TzDataDir(os.path.join(icuDir, "tools/tzcode"))
    (zones, links) = readIANAFiles(tzdir, ["icuzones"])

    # Remove the ICU placeholder time zone "Etc/Unknown".
    zones.remove(Zone("Etc/Unknown"))

    # tzdata2017c removed the link Canada/East-Saskatchewan -> America/Regina,
    # but it is still present in ICU sources. Manually tag it as a legacy time
    # zone so our tables are kept consistent with IANA.
    links[Zone("Canada/East-Saskatchewan")] = "America/Regina"

    return (zones, links)


def icuTzDataVersion(icuTzDir):
    """ Read the ICU time zone version from `icuTzDir`/zoneinfo64.txt. """
    def searchInFile(pattern, f):
        p = re.compile(pattern)
        for line in flines(f, "utf-8-sig"):
            m = p.search(line)
            if m:
                return m.group(1)
        return None

    zoneinfo = os.path.join(icuTzDir, "zoneinfo64.txt")
    if not os.path.isfile(zoneinfo):
        raise RuntimeError("file not found: %s" % zoneinfo)
    version = searchInFile("^//\s+tz version:\s+([0-9]{4}[a-z])$", zoneinfo)
    if version is None:
        raise RuntimeError("%s does not contain a valid tzdata version string" % zoneinfo)
    return version


def findIncorrectICUZones(ianaZones, ianaLinks, icuZones, icuLinks, ignoreBackzone):
    """ Find incorrect ICU zone entries. """
    def isIANATimeZone(zone): return zone in ianaZones or zone in ianaLinks

    def isICUTimeZone(zone): return zone in icuZones or zone in icuLinks

    def isICULink(zone): return zone in icuLinks

    # All IANA zones should be present in ICU.
    missingTimeZones = [zone for zone in ianaZones if not isICUTimeZone(zone)]
    # Normally zones in backzone are also present as links in one of the other
    # time zone files. The only exception to this rule is the Asia/Hanoi time
    # zone, this zone is only present in the backzone file.
    expectedMissing = [] if ignoreBackzone else [Zone("Asia/Hanoi")]
    if missingTimeZones != expectedMissing:
        raise RuntimeError("Not all zones are present in ICU, did you forget "
                           "to run intl/update-tzdata.sh? %s" % missingTimeZones)

    # Zones which are only present in ICU?
    additionalTimeZones = [zone for zone in icuZones if not isIANATimeZone(zone)]
    if additionalTimeZones:
        raise RuntimeError("Additional zones present in ICU, did you forget "
                           "to run intl/update-tzdata.sh? %s" % additionalTimeZones)

    # Zones which are marked as links in ICU.
    result = ((zone, icuLinks[zone]) for zone in ianaZones if isICULink(zone))

    # Remove unnecessary UTC mappings.
    utcnames = ["Etc/UTC", "Etc/UCT", "Etc/GMT"]
    result = ((zone, target) for (zone, target) in result if zone.name not in utcnames)

    return sorted(result, key=itemgetter(0))


def findIncorrectICULinks(ianaZones, ianaLinks, icuZones, icuLinks):
    """ Find incorrect ICU link entries. """
    def isIANATimeZone(zone): return zone in ianaZones or zone in ianaLinks

    def isICUTimeZone(zone): return zone in icuZones or zone in icuLinks

    def isICULink(zone): return zone in icuLinks

    def isICUZone(zone): return zone in icuZones

    # All links should be present in ICU.
    missingTimeZones = [zone for zone in ianaLinks.keys() if not isICUTimeZone(zone)]
    if missingTimeZones:
        raise RuntimeError("Not all zones are present in ICU, did you forget "
                           "to run intl/update-tzdata.sh? %s" % missingTimeZones)

    # Links which are only present in ICU?
    additionalTimeZones = [zone for zone in icuLinks.keys() if not isIANATimeZone(zone)]
    if additionalTimeZones:
        raise RuntimeError("Additional links present in ICU, did you forget "
                           "to run intl/update-tzdata.sh? %s" % additionalTimeZones)

    result = chain(
        # IANA links which have a different target in ICU.
        ((zone, target, icuLinks[zone]) for (zone, target) in ianaLinks.items()
         if isICULink(zone) and target != icuLinks[zone]),

        # IANA links which are zones in ICU.
        ((zone, target, zone.name) for (zone, target) in ianaLinks.items() if isICUZone(zone))
    )

    # Remove unnecessary UTC mappings.
    utcnames = ["Etc/UTC", "Etc/UCT", "Etc/GMT"]
    result = ((zone, target, icuTarget)
              for (zone, target, icuTarget) in result
              if target not in utcnames or icuTarget not in utcnames)

    return sorted(result, key=itemgetter(0))


generatedFileWarning = u"// Generated by make_intl_data.py. DO NOT EDIT."
tzdataVersionComment = u"// tzdata version = {0}"


def processTimeZones(tzdataDir, icuDir, icuTzDir, version, ignoreBackzone, ignoreFactory, out):
    """ Read the time zone info and create a new time zone cpp file. """
    print("Processing tzdata mapping...")
    (ianaZones, ianaLinks) = readIANATimeZones(tzdataDir, ignoreBackzone, ignoreFactory)
    (icuZones, icuLinks) = readICUTimeZones(icuDir, icuTzDir, ignoreFactory)
    (legacyZones, legacyLinks) = readICULegacyZones(icuDir)

    incorrectZones = findIncorrectICUZones(
        ianaZones, ianaLinks, icuZones, icuLinks, ignoreBackzone)
    if not incorrectZones:
        print("<<< No incorrect ICU time zones found, please update Intl.js! >>>")
        print("<<< Maybe https://ssl.icu-project.org/trac/ticket/12044 was fixed? >>>")

    incorrectLinks = findIncorrectICULinks(ianaZones, ianaLinks, icuZones, icuLinks)
    if not incorrectLinks:
        print("<<< No incorrect ICU time zone links found, please update Intl.js! >>>")
        print("<<< Maybe https://ssl.icu-project.org/trac/ticket/12044 was fixed? >>>")

    print("Writing Intl tzdata file...")
    with io.open(out, mode="w", encoding="utf-8", newline="") as f:
        println = partial(print, file=f)

        println(generatedFileWarning)
        println(tzdataVersionComment.format(version))
        println(u"")

        println(u"#ifndef builtin_intl_TimeZoneDataGenerated_h")
        println(u"#define builtin_intl_TimeZoneDataGenerated_h")
        println(u"")

        println(u"namespace js {")
        println(u"namespace timezone {")
        println(u"")

        println(u"// Format:")
        println(u'// "ZoneName" // ICU-Name [time zone file]')
        println(u"const char* const ianaZonesTreatedAsLinksByICU[] = {")
        for (zone, icuZone) in incorrectZones:
            println(u'    "%s", // %s [%s]' % (zone, icuZone, zone.filename))
        println(u"};")
        println(u"")

        println(u"// Format:")
        println(u'// "LinkName", "Target" // ICU-Target [time zone file]')
        println(u"struct LinkAndTarget")
        println(u"{")
        println(u"    const char* const link;")
        println(u"    const char* const target;")
        println(u"};")
        println(u"")
        println(u"const LinkAndTarget ianaLinksCanonicalizedDifferentlyByICU[] = {")
        for (zone, target, icuTarget) in incorrectLinks:
            println(u'    { "%s", "%s" }, // %s [%s]' % (zone, target, icuTarget, zone.filename))
        println(u"};")
        println(u"")

        println(u"// Legacy ICU time zones, these are not valid IANA time zone names. We also")
        println(u"// disallow the old and deprecated System V time zones.")
        println(u"// https://ssl.icu-project.org/repos/icu/trunk/icu4c/source/tools/tzcode/icuzones")  # NOQA: E501
        println(u"const char* const legacyICUTimeZones[] = {")
        for zone in chain(sorted(legacyLinks.keys()), sorted(legacyZones)):
            println(u'    "%s",' % zone)
        println(u"};")
        println(u"")

        println(u"} // namespace timezone")
        println(u"} // namespace js")
        println(u"")
        println(u"#endif /* builtin_intl_TimeZoneDataGenerated_h */")


def updateBackzoneLinks(tzdataDir, links):
    def withZone(fn): return lambda zone_target: fn(zone_target[0])

    (backzoneZones, backzoneLinks) = readIANAFiles(tzdataDir, ["backzone"])
    (stableZones, updatedLinks, updatedZones) = partition(
        links.items(),
        # Link not changed in backzone.
        withZone(lambda zone: zone not in backzoneLinks and zone not in backzoneZones),
        # Link has a new target.
        withZone(lambda zone: zone in backzoneLinks),
    )
    # Keep stable zones and links with updated target.
    return dict(chain(
                stableZones,
                map(withZone(lambda zone: (zone, backzoneLinks[zone])), updatedLinks)
                ))


def generateTzDataLinkTestContent(testDir, version, fileName, description, links):
    with io.open(os.path.join(testDir, fileName), mode="w", encoding="utf-8", newline="") as f:
        println = partial(print, file=f)

        println(u'// |reftest| skip-if(!this.hasOwnProperty("Intl"))')
        println(u"")
        println(generatedFileWarning)
        println(tzdataVersionComment.format(version))
        println(u"""
const tzMapper = [
    x => x,
    x => x.toUpperCase(),
    x => x.toLowerCase(),
];
""")

        println(description)
        println(u"const links = {")
        for (zone, target) in sorted(links, key=itemgetter(0)):
            println(u'    "%s": "%s",' % (zone, target))
        println(u"};")

        println(u"""
for (let [linkName, target] of Object.entries(links)) {
    if (target === "Etc/UTC" || target === "Etc/GMT")
        target = "UTC";

    for (let map of tzMapper) {
        let dtf = new Intl.DateTimeFormat(undefined, {timeZone: map(linkName)});
        let resolvedTimeZone = dtf.resolvedOptions().timeZone;
        assertEq(resolvedTimeZone, target, `${linkName} -> ${target}`);
    }
}
""")
        println(u"""
if (typeof reportCompare === "function")
    reportCompare(0, 0, "ok");
""")


def generateTzDataTestBackwardLinks(tzdataDir, version, ignoreBackzone, testDir):
    (zones, links) = readIANAFiles(tzdataDir, ["backward"])
    assert len(zones) == 0

    if not ignoreBackzone:
        links = updateBackzoneLinks(tzdataDir, links)

    generateTzDataLinkTestContent(
        testDir, version,
        "timeZone_backward_links.js",
        u"// Link names derived from IANA Time Zone Database, backward file.",
        links.items()
    )


def generateTzDataTestNotBackwardLinks(tzdataDir, version, ignoreBackzone, testDir):
    tzfiles = filterfalse({"backward", "backzone"}.__contains__, listIANAFiles(tzdataDir))
    (zones, links) = readIANAFiles(tzdataDir, tzfiles)

    if not ignoreBackzone:
        links = updateBackzoneLinks(tzdataDir, links)

    generateTzDataLinkTestContent(
        testDir, version,
        "timeZone_notbackward_links.js",
        u"// Link names derived from IANA Time Zone Database, excluding backward file.",
        links.items()
    )


def generateTzDataTestBackzone(tzdataDir, version, ignoreBackzone, testDir):
    backzoneFiles = {"backzone"}
    (bkfiles, tzfiles) = partition(listIANAFiles(tzdataDir), backzoneFiles.__contains__)

    # Read zone and link infos.
    (zones, links) = readIANAFiles(tzdataDir, tzfiles)
    (backzones, backlinks) = readIANAFiles(tzdataDir, bkfiles)

    if not ignoreBackzone:
        comment = u"""\
// This file was generated with historical, pre-1970 backzone information
// respected. Therefore, every zone key listed below is its own Zone, not
// a Link to a modern-day target as IANA ignoring backzones would say.

"""
    else:
        comment = u"""\
// This file was generated while ignoring historical, pre-1970 backzone
// information. Therefore, every zone key listed below is part of a Link
// whose target is the corresponding value.

"""

    generateTzDataLinkTestContent(
        testDir, version,
        "timeZone_backzone.js",
        comment + u"// Backzone zones derived from IANA Time Zone Database.",
        ((zone, zone if not ignoreBackzone else links[zone])
         for zone in backzones if zone in links)
    )


def generateTzDataTestBackzoneLinks(tzdataDir, version, ignoreBackzone, testDir):
    backzoneFiles = {"backzone"}
    (bkfiles, tzfiles) = partition(listIANAFiles(tzdataDir), backzoneFiles.__contains__)

    # Read zone and link infos.
    (zones, links) = readIANAFiles(tzdataDir, tzfiles)
    (backzones, backlinks) = readIANAFiles(tzdataDir, bkfiles)

    if not ignoreBackzone:
        comment = u"""\
// This file was generated with historical, pre-1970 backzone information
// respected. Therefore, every zone key listed below points to a target
// in the backzone file and not to its modern-day target as IANA ignoring
// backzones would say.

"""
    else:
        comment = u"""\
// This file was generated while ignoring historical, pre-1970 backzone
// information. Therefore, every zone key listed below is part of a Link
// whose target is the corresponding value ignoring any backzone entries.

"""

    generateTzDataLinkTestContent(
        testDir, version,
        "timeZone_backzone_links.js",
        comment + u"// Backzone links derived from IANA Time Zone Database.",
        ((zone, target if not ignoreBackzone else links[zone])
         for (zone, target) in backlinks.items())
    )


def generateTzDataTests(tzdataDir, version, ignoreBackzone, testDir):
    generateTzDataTestBackwardLinks(tzdataDir, version, ignoreBackzone, testDir)
    generateTzDataTestNotBackwardLinks(tzdataDir, version, ignoreBackzone, testDir)
    generateTzDataTestBackzone(tzdataDir, version, ignoreBackzone, testDir)
    generateTzDataTestBackzoneLinks(tzdataDir, version, ignoreBackzone, testDir)


def updateTzdata(topsrcdir, args):
    """ Update the time zone cpp file. """

    icuDir = os.path.join(topsrcdir, "intl/icu/source")
    if not os.path.isdir(icuDir):
        raise RuntimeError("not a directory: %s" % icuDir)

    icuTzDir = os.path.join(topsrcdir, "intl/tzdata/source")
    if not os.path.isdir(icuTzDir):
        raise RuntimeError("not a directory: %s" % icuTzDir)

    dateTimeFormatTestDir = os.path.join(topsrcdir, "js/src/tests/non262/Intl/DateTimeFormat")
    if not os.path.isdir(dateTimeFormatTestDir):
        raise RuntimeError("not a directory: %s" % dateTimeFormatTestDir)

    tzDir = args.tz
    if tzDir is not None and not (os.path.isdir(tzDir) or os.path.isfile(tzDir)):
        raise RuntimeError("not a directory or file: %s" % tzDir)
    ignoreBackzone = args.ignore_backzone
    # TODO: Accept or ignore the placeholder time zone "Factory"?
    ignoreFactory = False
    out = args.out

    version = icuTzDataVersion(icuTzDir)
    url = "https://www.iana.org/time-zones/repository/releases/tzdata%s.tar.gz" % version

    print("Arguments:")
    print("\ttzdata version: %s" % version)
    print("\ttzdata URL: %s" % url)
    print("\ttzdata directory|file: %s" % tzDir)
    print("\tICU directory: %s" % icuDir)
    print("\tICU timezone directory: %s" % icuTzDir)
    print("\tIgnore backzone file: %s" % ignoreBackzone)
    print("\tOutput file: %s" % out)
    print("")

    def updateFrom(f):
        if os.path.isfile(f) and tarfile.is_tarfile(f):
            with tarfile.open(f, "r:*") as tar:
                processTimeZones(TzDataFile(tar), icuDir, icuTzDir, version,
                                 ignoreBackzone, ignoreFactory, out)
                generateTzDataTests(TzDataFile(tar), version,
                                    ignoreBackzone, dateTimeFormatTestDir)
        elif os.path.isdir(f):
            processTimeZones(TzDataDir(f), icuDir, icuTzDir, version,
                             ignoreBackzone, ignoreFactory, out)
            generateTzDataTests(TzDataDir(f), version, ignoreBackzone, dateTimeFormatTestDir)
        else:
            raise RuntimeError("unknown format")

    if tzDir is None:
        print("Downloading tzdata file...")
        with closing(urlopen(url)) as tzfile:
            fname = urlsplit(tzfile.geturl()).path.split("/")[-1]
            with tempfile.NamedTemporaryFile(suffix=fname) as tztmpfile:
                print("File stored in %s" % tztmpfile.name)
                tztmpfile.write(tzfile.read())
                tztmpfile.flush()
                updateFrom(tztmpfile.name)
    else:
        updateFrom(tzDir)


def readCurrencyFile(tree):
    reCurrency = re.compile(r"^[A-Z]{3}$")
    reIntMinorUnits = re.compile(r"^\d+$")

    for country in tree.iterfind(".//CcyNtry"):
        # Skip entry if no currency information is available.
        currency = country.findtext("Ccy")
        if currency is None:
            continue
        assert reCurrency.match(currency)

        minorUnits = country.findtext("CcyMnrUnts")
        assert minorUnits is not None

        # Skip all entries without minorUnits or which use the default minorUnits.
        if reIntMinorUnits.match(minorUnits) and int(minorUnits) != 2:
            currencyName = country.findtext("CcyNm")
            countryName = country.findtext("CtryNm")
            yield (currency, int(minorUnits), currencyName, countryName)


def writeCurrencyFile(published, currencies, out):
    with io.open(out, mode="w", encoding="utf-8", newline="") as f:
        println = partial(print, file=f)

        println(generatedFileWarning)
        println(u"// Version: {}".format(published))

        println(u"""
/**
 * Mapping from currency codes to the number of decimal digits used for them.
 * Default is 2 digits.
 *
 * Spec: ISO 4217 Currency and Funds Code List.
 * http://www.currency-iso.org/en/home/tables/table-a1.html
 */""")
        println(u"var currencyDigits = {")
        for (currency, entries) in groupby(sorted(currencies, key=itemgetter(0)), itemgetter(0)):
            for (_, minorUnits, currencyName, countryName) in entries:
                println(u"    // {} ({})".format(currencyName, countryName))
            println(u"    {}: {},".format(currency, minorUnits))
        println(u"};")


def updateCurrency(topsrcdir, args):
    """ Update the CurrencyDataGenerated.js file. """
    import xml.etree.ElementTree as ET
    from random import randint

    url = args.url
    out = args.out
    filename = args.file

    print("Arguments:")
    print("\tDownload url: %s" % url)
    print("\tLocal currency file: %s" % filename)
    print("\tOutput file: %s" % out)
    print("")

    def updateFrom(currencyFile):
        print("Processing currency code list file...")
        tree = ET.parse(currencyFile)
        published = tree.getroot().attrib["Pblshd"]
        currencies = readCurrencyFile(tree)

        print("Writing CurrencyData file...")
        writeCurrencyFile(published, currencies, out)

    if filename is not None:
        print("Always make sure you have the newest currency code list file!")
        updateFrom(filename)
    else:
        print("Downloading currency & funds code list...")
        request = UrlRequest(url)
        request.add_header(
            "User-agent", "Mozilla/5.0 (Mobile; rv:{0}.0) Gecko/{0}.0 Firefox/{0}.0".format(
                randint(1, 999)))
        with closing(urlopen(request)) as currencyFile:
            fname = urlsplit(currencyFile.geturl()).path.split("/")[-1]
            with tempfile.NamedTemporaryFile(suffix=fname) as currencyTmpFile:
                print("File stored in %s" % currencyTmpFile.name)
                currencyTmpFile.write(currencyFile.read())
                currencyTmpFile.flush()
                updateFrom(currencyTmpFile.name)


def writeUnicodeExtensionsFile(version, url, mapping, out):
    with io.open(out, mode="w", encoding="utf-8", newline="") as f:
        println = partial(print, file=f)

        println(generatedFileWarning)
        println(u"// Version: CLDR-{}".format(version))
        println(u"// URL: {}".format(url))

        println(u"""
/**
 * Mapping from deprecated BCP 47 Unicode extension types to their preferred
 * values.
 *
 * Spec: https://www.unicode.org/reports/tr35/#Unicode_Locale_Extension_Data_Files
 */""")
        println(u"var deprecatedUnicodeExtensionTypes = {")
        for ext_name in sorted(mapping):
            println(u"    {}: {{".format(ext_name))
            is_first = True
            for type in sorted(mapping[ext_name]):
                mapped = mapping[ext_name][type]
                has_description = mapped["description"] is not None

                if not is_first and has_description:
                    println(u"")
                is_first = False

                if has_description:
                    println(u"        // {}".format(mapped["description"]))
                println(u"        \"{}\": \"{}\",".format(type, mapped["preferred"]))
            println(u"    },")
        println(u"};")


def updateUnicodeExtensions(args):
    """ Update the UnicodeExtensionsGenerated.js file. """

    import xml.etree.ElementTree as ET

    version = args.version
    url = args.url
    out = args.out
    filename = args.file

    url = url.replace("<VERSION>", version)

    print("Arguments:")
    print("\tCLDR version: %s" % version)
    print("\tDownload url: %s" % url)
    if filename is not None:
        print("\tLocal CLDR core.zip file: %s" % filename)
    print("\tOutput file: %s" % out)
    print("")

    def updateFrom(data):
        # Match all xml-files in the BCP 47 directory.
        bcpFileRE = re.compile(r"^common/bcp47/.+\.xml$")

        # https://www.unicode.org/reports/tr35/#Unicode_locale_identifier
        #
        # type = alphanum{3,8} (sep alphanum{3,8})* ;
        typeRE = re.compile(r"^[a-z0-9]{3,8}(-[a-z0-9]{3,8})*$")

        # Mapping from Unicode extension types to dict of deprecated to
        # preferred values.
        mapping = {}

        with ZipFile(data) as zip_file:
            for name in zip_file.namelist():
                if not bcpFileRE.match(name):
                    continue

                tree = ET.parse(zip_file.open(name))
                for keyword in tree.iterfind(".//keyword/key"):
                    # Skip over keywords whose extension is not "u".
                    if keyword.get("extension", "u") != "u":
                        continue

                    extension_name = keyword.get("name")

                    for type in keyword.iterfind("type"):
                        # <https://unicode.org/reports/tr35/#Unicode_Locale_Extension_Data_Files>:
                        #
                        # The key or type name used by Unicode locale extension with 'u' extension
                        # syntax or the 't' extensions syntax. When alias below is absent, this
                        # name can be also used with the old style "@key=type" syntax.
                        name = type.get("name")

                        # Ignore the special name:
                        # - <https://unicode.org/reports/tr35/#CODEPOINTS>
                        # - <https://unicode.org/reports/tr35/#REORDER_CODE>
                        # - <https://unicode.org/reports/tr35/#RG_KEY_VALUE>
                        # - <https://unicode.org/reports/tr35/#SUBDIVISION_CODE>
                        # - <https://unicode.org/reports/tr35/#PRIVATE_USE>
                        if name in ("CODEPOINTS", "REORDER_CODE", "RG_KEY_VALUE",
                                    "SUBDIVISION_CODE", "PRIVATE_USE"):
                            continue

                        # All other names should match the 'type' production.
                        assert typeRE.match(name) is not None, (
                               "{} matches the 'type' production".format(name))

                        # <https://unicode.org/reports/tr35/#Unicode_Locale_Extension_Data_Files>:
                        #
                        # The preferred value of the deprecated key, type or attribute element.
                        # When a key, type or attribute element is deprecated, this attribute is
                        # used for specifying a new canonical form if available.
                        preferred = type.get("preferred")

                        # <https://unicode.org/reports/tr35/#Unicode_Locale_Extension_Data_Files>:
                        #
                        # The BCP 47 form is the canonical form, and recommended. Other aliases are
                        # included only for backwards compatibility.
                        alias = type.get("alias")

                        # <https://unicode.org/reports/tr35/#Unicode_Locale_Extension_Data_Files>:
                        #
                        # The description of the key, type or attribute element.
                        description = type.get("description")

                        # <https://unicode.org/reports/tr35/#Canonical_Unicode_Locale_Identifiers>
                        #
                        # Use the bcp47 data to replace keys, types, tfields, and tvalues by their
                        # canonical forms. See Section 3.6.4 U Extension Data Files) and Section
                        # 3.7.1 T Extension Data Files. The aliases are in the alias attribute
                        # value, while the canonical is in the name attribute value.

                        # 'preferred' contains the new preferred name, 'alias' the compatibility
                        # name, but then there's this entry where 'preferred' and 'alias' are the
                        # same. So which one to choose? Assume 'preferred' is the actual canonical
                        # name.
                        #
                        # <type name="islamicc"
                        #       description="Civil (algorithmic) Arabic calendar"
                        #       deprecated="true"
                        #       preferred="islamic-civil"
                        #       alias="islamic-civil"/>

                        if preferred is not None:
                            assert typeRE.match(preferred), preferred
                            mapping.setdefault(extension_name, {})[name] = {
                                "preferred": preferred,
                                "description": description,
                            }

                        if alias is not None:
                            for alias_name in alias.lower().split(" "):
                                # Ignore alias entries which don't match the 'type' production.
                                if typeRE.match(alias_name) is None:
                                    continue

                                # See comment above when 'alias' and 'preferred' are both present.
                                if (preferred is not None and
                                    name in mapping[extension_name]):
                                    continue

                                # Skip over entries where 'name' and 'alias' are equal.
                                #
                                # <type name="pst8pdt"
                                #       description="POSIX style time zone for US Pacific Time"
                                #       alias="PST8PDT"
                                #       since="1.8"/>
                                if name == alias_name:
                                    continue

                                mapping.setdefault(extension_name, {})[alias_name] = {
                                    "preferred": name,
                                    "description": description,
                                }

            # Find subdivision and region replacements.
            #
            # <https://www.unicode.org/reports/tr35/#Canonical_Unicode_Locale_Identifiers>
            #
            # Replace aliases in special key values:
            #   - If there is an 'sd' or 'rg' key, replace any subdivision alias
            #     in its value in the same way, using subdivisionAlias data.
            tree = ET.parse(zip_file.open("common/supplemental/supplementalMetadata.xml"))
            for alias in tree.iterfind(".//subdivisionAlias"):
                type = alias.get("type")
                assert typeRE.match(type) is not None, (
                       "{} matches the 'type' production".format(type))

                # Take the first replacement when multiple ones are present.
                replacement = alias.get("replacement").split(" ")[0].lower()

                # Skip over invalid replacements.
                #
                # <subdivisionAlias type="fi01" replacement="AX" reason="overlong"/>
                #
                # It's not entirely clear to me if CLDR actually wants to use
                # "axzzzz" as the replacement for this case.
                if typeRE.match(replacement) is None:
                    continue

                # 'subdivisionAlias' applies to 'rg' and 'sd' keys.
                mapping.setdefault("rg", {})[type] = {
                    "preferred": replacement,
                    "description": None,
                }
                mapping.setdefault("sd", {})[type] = {
                    "preferred": replacement,
                    "description": None,
                }

        writeUnicodeExtensionsFile(version, url, mapping, out)

    if filename is not None:
        print("Always make sure you have the newest CLDR core.zip!")
        with open(filename, "rb") as cldr_file:
            updateFrom(cldr_file)
    else:
        print("Downloading CLDR core.zip...")
        with closing(urlopen(url)) as cldr_file:
            cldr_data = io.BytesIO(cldr_file.read())
            updateFrom(cldr_data)


if __name__ == "__main__":
    import argparse

    # This script must reside in js/src/builtin/intl to work correctly.
    (thisDir, thisFile) = os.path.split(os.path.abspath(sys.argv[0]))
    dirPaths = os.path.normpath(thisDir).split(os.sep)
    if "/".join(dirPaths[-4:]) != "js/src/builtin/intl":
        raise RuntimeError("%s must reside in js/src/builtin/intl" % sys.argv[0])
    topsrcdir = "/".join(dirPaths[:-4])

    def EnsureHttps(v):
        if not v.startswith("https:"):
            raise argparse.ArgumentTypeError("URL protocol must be https: " % v)
        return v

    parser = argparse.ArgumentParser(description="Update intl data.")
    subparsers = parser.add_subparsers(help="Select update mode")

    parser_cldr_tags = subparsers.add_parser("langtags",
                                             help="Update CLDR language tags data")
    parser_cldr_tags.add_argument("--url",
                                  metavar="URL",
                                  default="https://github.com/unicode-org/cldr.git",
                                  help="URL to git repository (default: %(default)s)")
    parser_cldr_tags.add_argument("--branch", default="latest",
                                  help="Git branch (default: %(default)s)")
    parser_cldr_tags.add_argument("--revision", default="HEAD",
                                  help="Git revision (default: %(default)s)")
    parser_cldr_tags.add_argument("--out",
                                  default="LangTagMappingsGenerated.js",
                                  help="Output file (default: %(default)s)")
    parser_cldr_tags.add_argument("files",
                                  nargs="*",
                                  help="Local ldmlSupplemental.dtd, supplementalMetadata.xml, "
                                       "and likelySubtags.xml files, if omitted uses <URL>")
    parser_cldr_tags.set_defaults(func=updateCLDRLangTags)

    parser_tz = subparsers.add_parser("tzdata", help="Update tzdata")
    parser_tz.add_argument("--tz",
                           help="Local tzdata directory or file, if omitted downloads tzdata "
                                "distribution from https://www.iana.org/time-zones/")
    # ICU doesn't include the backzone file by default, but we still like to
    # use the backzone time zone names to avoid user confusion. This does lead
    # to formatting "historic" dates (pre-1970 era) with the wrong time zone,
    # but that's probably acceptable for now.
    parser_tz.add_argument("--ignore-backzone",
                           action="store_true",
                           help="Ignore tzdata's 'backzone' file. Can be enabled to generate more "
                                "accurate time zone canonicalization reflecting the actual time "
                                "zones as used by ICU.")
    parser_tz.add_argument("--out",
                           default="TimeZoneDataGenerated.h",
                           help="Output file (default: %(default)s)")
    parser_tz.set_defaults(func=partial(updateTzdata, topsrcdir))

    parser_currency = subparsers.add_parser("currency", help="Update currency digits mapping")
    parser_currency.add_argument("--url",
                                 metavar="URL",
                                 default="https://www.currency-iso.org/dam/downloads/lists/list_one.xml",  # NOQA: E501
                                 type=EnsureHttps,
                                 help="Download url for the currency & funds code list (default: "
                                      "%(default)s)")
    parser_currency.add_argument("--out",
                                 default="CurrencyDataGenerated.js",
                                 help="Output file (default: %(default)s)")
    parser_currency.add_argument("file",
                                 nargs="?",
                                 help="Local currency code list file, if omitted uses <URL>")
    parser_currency.set_defaults(func=partial(updateCurrency, topsrcdir))

    parser_unicode_ext = subparsers.add_parser("unicode-ext", help="Update Unicode extensions")
    parser_unicode_ext.add_argument("--version",
                                    metavar="VERSION",
                                    required=True,
                                    help="CLDR version number")
    parser_unicode_ext.add_argument("--url",
                                    metavar="URL",
                                    default="https://unicode.org/Public/cldr/<VERSION>/core.zip",
                                    type=EnsureHttps,
                                    help="Download url CLDR data (default: %(default)s)")
    parser_unicode_ext.add_argument("--out",
                                    default="UnicodeExtensionsGenerated.js",
                                    help="Output file (default: %(default)s)")
    parser_unicode_ext.add_argument("file",
                                    nargs="?",
                                    help="Local cldr-core.zip file, if omitted uses <URL>")
    parser_unicode_ext.set_defaults(func=updateUnicodeExtensions)

    args = parser.parse_args()
    args.func(args)
