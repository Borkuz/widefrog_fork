import builtins
import importlib
import json
import os
import sys
from urllib.parse import urlparse

from utils.constants.macros import CONFIG_FILE, INF_MSG, DEFAULT_DEBUG_MODE


def test_service():
    from utils.main_service import main_service

    main_service.run_service(
        # _35mm_online.DEMO_URLS +
        # adtv_ae.DEMO_URLS +
        # adultswim_com.DEMO_URLS +
        # aloula_sa.DEMO_URLS +
        # app_nzrplus_com.DEMO_URLS +
        # auvio_rtbf_be.DEMO_URLS +
        # canalplus_com.DEMO_URLS +
        # cbs_com.DEMO_URLS +
        # cda_pl.DEMO_URLS +
        # channel4_com.DEMO_URLS +
        # cineverse_com.DEMO_URLS +
        # docplus_com.DEMO_URLS +
        # eurovisionsport_com.DEMO_URLS +
        # fifa_com.DEMO_URLS +
        # filmzie_com.DEMO_URLS +
        # france_tv.DEMO_URLS +
        # goplay_be.DEMO_URLS +
        # joyn_de.DEMO_URLS +
        # kanopy_com.DEMO_URLS +
        # kijk_nl.DEMO_URLS +
        # m6_fr.DEMO_URLS +
        # maoriplus_co_nz.DEMO_URLS +
        # mediasetinfinity_mediaset_it.DEMO_URLS +
        # midnightpulp_com.DEMO_URLS +
        # mtv_fi.DEMO_URLS +
        # nba_com.DEMO_URLS +
        # nemzetiarchivum_hu.DEMO_URLS +
        # netflix_com.DEMO_URLS +
        # ninateka_pl.DEMO_URLS +
        # npo_nl.DEMO_URLS +
        # pianogroove_com.DEMO_URLS +
        # play_tv3_lt.DEMO_URLS +
        # play_tv3_lv.DEMO_URLS +
        # play_virginmediatelevision_ie.DEMO_URLS +
        # play_xumo_com.DEMO_URLS +
        # player_pl.DEMO_URLS +
        # plus_fifa_com.DEMO_URLS +
        # plus_rtl_de.DEMO_URLS +
        # ptvflix_org.DEMO_URLS +
        # rakuten_tv.DEMO_URLS +
        # rmcbfmplay_com.DEMO_URLS +
        # rsi_ch.DEMO_URLS +
        # rtlplay_be.DEMO_URLS +
        # rtp_pt.DEMO_URLS +
        # rts_ch.DEMO_URLS +
        # rtve_es.DEMO_URLS +
        # rugbypass_tv.DEMO_URLS +
        # shahid_mbc_net.DEMO_URLS +
        # stirr_com.DEMO_URLS +
        # tf1_fr.DEMO_URLS +
        # tg4_ie.DEMO_URLS +
        # threenow_co_nz.DEMO_URLS +
        # tv5mondeplus_com.DEMO_URLS +
        # uefa_tv.DEMO_URLS +
        # veeps_com.DEMO_URLS +
        # video_telequebec_tv.DEMO_URLS +
        # viki_com.DEMO_URLS +
        # viu_com.DEMO_URLS +
        # vix_com.DEMO_URLS +
        # vrt_be.DEMO_URLS +
        # vtmgo_be.DEMO_URLS +
        # watch_blaze_tv.DEMO_URLS +
        # watch_globaltv_com.DEMO_URLS +
        # watch_shortly_film.DEMO_URLS +
        # watch_tbn_uk.DEMO_URLS +
        # wittytv_it.DEMO_URLS +
        # zee5_com.DEMO_URLS +
        []
    )
    sys.exit(0)


def first_setup(arguments):
    from utils.tools.args import get_config, check_query_args
    from utils.tools.service import get_all_services

    is_first = not os.path.exists(CONFIG_FILE)
    builtins.CONFIG = get_config(arguments)
    builtins.CONFIG["QUERY"] = check_query_args(arguments)

    if "--debug" in arguments:
        builtins.CONFIG["DEBUG_MODE"] = True
    else:
        builtins.CONFIG["DEBUG_MODE"] = DEFAULT_DEBUG_MODE
    if "--fresh" in arguments:
        builtins.CONFIG["FRESH"] = True

    builtins.SERVICES = get_all_services()
    return is_first


def run_catalog_index(arguments):
    source_urls = [argument for argument in arguments if not argument.startswith("--")]
    if len(source_urls) != 1:
        sys.exit("The --index option requires exactly one URL.")

    url = source_urls[0]
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname is None
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        sys.exit("The --index option requires one HTTPS service URL.")

    service_name = parsed_url.hostname
    if service_name.startswith("www."):
        service_name = service_name[4:]
    service_name = service_name.replace(".", "_")
    module_name = f"services.{service_name}"
    try:
        service_module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name == module_name:
            sys.exit(f"No indexer service is available for {parsed_url.hostname}.")
        raise

    service = getattr(service_module, service_name, None)
    if service is None:
        sys.exit(f"The {parsed_url.hostname} module is not a valid service.")
    service = service.initialize_service()
    index_page = getattr(service, "index_page", None)
    if not callable(index_page):
        sys.exit(f"The {service.__name__} service does not support catalog indexing.")

    try:
        print(json.dumps(index_page(url), ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError) as error:
        sys.exit(f"Failed to index {url}: {error}")


if __name__ == '__main__':
    args = sys.argv[1:]
    if "--index" in args:
        run_catalog_index(args)
        sys.exit(0)

    from utils.tools.args import (
        check_help_args,
        check_input_args,
        check_tool_args,
    )
    from utils.main_service import main_service
    from utils.tools.service import get_all_services

    response = check_help_args(args)
    if response is not None and len(response) > 0:
        print(response)
        sys.exit(0)

    if first_setup(args):
        print(INF_MSG.format(
            msg=f"The {CONFIG_FILE} file was created. Edit the file and run the program."
        ))
        sys.exit(0)

    bad_args = check_tool_args(builtins.CONFIG["DOWNLOAD_COMMANDS"]["TOOL_PARAMETERS"])
    if len(bad_args) > 0:
        print("Those arguments are not allowed: ", ", ".join(bad_args))
        sys.exit(0)

    # test_service()

    response = check_input_args(args)
    if len(response) == 0:
        sys.exit(0)
    main_service.run_service(response)
