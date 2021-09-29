import json
import logging

from folioclient import FolioClient
from marc_to_folio.custom_exceptions import (
    TransformationRecordFailedError,
    TransformationProcessError,
)


class RefDataMapping(object):
    def __init__(
        self, folio_client: FolioClient, ref_data_path, array_name, map, key_type
    ):
        self.name = array_name
        logging.info(f"{self.name} reference data mapping. Initializing")
        logging.info(f"Fetching {self.name} reference data from FOLIO")
        self.ref_data = list(folio_client.folio_get_all(ref_data_path, array_name))
        # logging.debug(json.dumps(self.ref_data, indent=4))
        self.map = map
        self.regular_mappings = []
        self.key_type = key_type
        self.hybrid_mappings = []
        self.mapped_legacy_keys = []
        self.default_name = ""
        self.cached_dict = {}
        self.setup_mappings()
        logging.info(f"{self.name} reference data mapping. Done init")

    def get_ref_data_tuple(self, key_value):
        ref_object = self.cached_dict.get(key_value.lower().strip(), ())
        if ref_object:
            return ref_object
        self.cached_dict = {
            r[self.key_type].lower(): (r["id"], r["name"]) for r in self.ref_data
        }
        return self.cached_dict.get(key_value.lower().strip(), ())

    def setup_mappings(self):
        self.pre_validate_map()
        for idx, mapping in enumerate(self.map):
            try:
                # Get the legacy keys
                if idx == 0:
                    self.mapped_legacy_keys = get_mapped_legacy_keys(mapping)
                if self.is_default_mapping(mapping):
                    # Set up default mapping if available
                    t = self.get_ref_data_tuple(mapping[f"folio_{self.key_type}"])
                    if t:
                        self.default_id = t[0]
                        self.default_name = t[1]
                        logging.info(
                            f'Set {mapping[f"folio_{self.key_type}"]} as default {self.name} mapping'
                        )
                    else:
                        x = mapping.get(f"folio_{self.key_type}", "")
                        raise TransformationProcessError(
                            f"No {self.name} - {x} - set up in map or tenant. Check for inconstencies in {self.name} naming."
                            f"Add a row to mapping file with *:s and a valid {self.name}"
                        )
                else:
                    if self.is_hybrid_default_mapping(mapping):
                        self.hybrid_mappings.append(mapping)
                    else:
                        self.regular_mappings.append(mapping)
                    t = self.get_ref_data_tuple(mapping[f"folio_{self.key_type}"])
                    if not t:
                        raise TransformationProcessError(
                            f"Mapping not found for {mapping}"
                        )
                    mapping["folio_id"] = t[0]
            except TransformationProcessError as te:
                raise te
            except Exception as ee:
                logging.info(json.dumps(self.map, indent=4))
                logging.exception()
                raise TransformationProcessError(
                    f'{mapping[f"folio_{self.key_type}"]} could not be found in FOLIO'
                )
        self.post_validate_map()
        logging.info(
            f"Loaded {len(self.regular_mappings)} mappings for {len(self.ref_data)} {self.name} in FOLIO"
        )
        logging.info(
            f"loaded {len(self.hybrid_mappings)} hybrid mappings for {len(self.ref_data)} {self.name} in FOLIO"
        )

    def is_hybrid_default_mapping(self, mapping):
        legacy_values = [
            value for key, value in mapping.items() if key in self.mapped_legacy_keys
        ]
        return "*" in legacy_values and not self.is_default_mapping(mapping)

    def is_default_mapping(self, mapping):
        legacy_values = [
            value for key, value in mapping.items() if key in self.mapped_legacy_keys
        ]
        return all(f == "*" for f in legacy_values)

    def pre_validate_map(self):
        if not any(f for f in self.map if f.get(f"folio_{self.key_type}", "")):
            raise TransformationProcessError(
                f"Column folio_{self.key_type} missing from {self.name} map file"
            )
        folio_values_from_map = [f[f"folio_{self.key_type}"] for f in self.map]
        folio_values_from_folio = [r[self.key_type] for r in self.ref_data]
        folio_values_not_in_map = list(
            {f for f in folio_values_from_folio if f not in folio_values_from_map}
        )
        map_values_not_in_folio = list(
            {f for f in folio_values_from_map if f not in folio_values_from_folio}
        )
        if any(folio_values_not_in_map):
            logging.info(
                f"Values from {self.name} ref data in FOLIO that are not in the map: {folio_values_not_in_map}"
            )
        if any(map_values_not_in_folio):
            raise TransformationProcessError(
                f"Values from {self.name} map are not in FOLIO: {map_values_not_in_folio}"
            )

    def post_validate_map(self):
        if not self.default_id:
            raise TransformationProcessError(
                f"No default {self.name} set up in map."
                f"Add a row to mapping file with *:s in all legacy columns and a valid {self.name} value"
            )
        for mapping in self.map:
            if f"folio_{self.key_type}" not in mapping:
                logging.critical(
                    f"folio_{self.key_type} is not a column in the {self.name} mapping file. Fix."
                )
                exit()
            elif (
                all(k not in mapping for k in self.mapped_legacy_keys)
                and "legacy_code" not in mapping
            ):
                logging.critical(
                    f"field names from {self.mapped_legacy_keys} missing in map legacy_code is not a column in the {self.name} mapping file"
                )
                exit()
            elif not all(mapping.values()):
                logging.critical(
                    f"empty value in mapping {mapping.values()}. Check {self.name} mapping file"
                )
                exit()


def get_mapped_legacy_keys(mapping):
    legacy_keys = [
        k
        for k in mapping.keys()
        if k not in ["folio_code", "folio_id", "folio_name", "legacy_code"]
    ]
    # logging.info(json.dumps(legacy_keys, indent=4))
    return legacy_keys
