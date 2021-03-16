import csv
import json
from marc_to_folio.custom_exceptions import (
    TransformationDataError,
    TransformationProcessError,
)
import uuid
from abc import abstractmethod
import requests
import re
from folioclient import FolioClient
import os


class MapperBase:
    def __init__(self, folio_client: FolioClient, schema, record_map, error_file):
        self.schema = schema
        self.stats = {}
        self.migration_report = {}
        self.folio_client = folio_client
        self.mapped_folio_fields = {}
        self.mapped_legacy_fields = {}
        self.use_map = True  # Legacy
        self.record_map = record_map
        self.error_file = error_file
        self.ref_data_dicts = {}
        csv.register_dialect("tsv", delimiter="\t")

    def write_migration_report(self, report_file):
        print("Writing migration report")
        for a in self.migration_report:
            report_file.write(f"   \n")
            report_file.write(f"## {a}    \n")
            report_file.write(
                f"<details><summary>Click to expand all {len(self.migration_report[a])} things</summary>     \n"
            )
            report_file.write(f"   \n")
            report_file.write(f"Measure | Count   \n")
            report_file.write(f"--- | ---:   \n")
            b = self.migration_report[a]
            sortedlist = [(k, b[k]) for k in sorted(b, key=as_str)]
            for b in sortedlist:
                report_file.write(f"{b[0]} | {b[1]}   \n")
            report_file.write("</details>   \n")

    def print_mapping_report(self, report_file, total_records):
        print("Writing mapping report")
        report_file.write("\n## Mapped FOLIO fields   \n")
        d_sorted = {
            k: self.mapped_folio_fields[k] for k in sorted(self.mapped_folio_fields)
        }
        report_file.write(f"FOLIO Field | Mapped | Empty | Unmapped  \n")
        report_file.write("--- | --- | --- | ---:  \n")
        for k, v in d_sorted.items():
            unmapped = total_records - v[0]
            mapped = v[0] - v[1]
            mp = mapped / total_records
            mapped_per = "{:.0%}".format(mp if mp > 0 else 0)
            report_file.write(
                f"{k} | {mapped if mapped > 0 else 0} ({mapped_per}) | {v[1]} | {unmapped}  \n"
            )

        # Legacy fields (like marc)
        report_file.write("\n## Mapped Legacy fields  \n")
        d_sorted = {
            k: self.mapped_legacy_fields[k] for k in sorted(self.mapped_legacy_fields)
        }
        report_file.write(f"Legacy Field | Present | Mapped | Empty | Unmapped  \n")
        report_file.write("--- | --- | --- | --- | ---:  \n")
        for k, v in d_sorted.items():
            present = v[0]
            present_per = "{:.1%}".format(present / total_records)
            unmapped = present - v[1]
            mapped = v[1]
            mp = mapped / total_records
            mapped_per = "{:.0%}".format(mp if mp > 0 else 0)
            report_file.write(
                f"{k} | {present if present > 0 else 0} ({present_per}) | {mapped if mapped > 0 else 0} ({mapped_per}) | {v[1]} | {unmapped}  \n"
            )

    def report_legacy_mapping(self, field_name, was_mapped, was_empty=False):
        if field_name not in self.mapped_legacy_fields:
            self.mapped_legacy_fields[field_name] = [int(was_mapped), int(was_empty)]
        else:
            self.mapped_legacy_fields[field_name][0] += int(was_mapped)
            self.mapped_legacy_fields[field_name][1] += int(was_empty)

    def report_folio_mapping(self, field_name, transformed, was_empty=False):
        if field_name not in self.mapped_folio_fields:
            self.mapped_folio_fields[field_name] = [int(transformed), int(was_empty)]
        else:
            self.mapped_folio_fields[field_name][0] += int(transformed)
            self.mapped_folio_fields[field_name][1] += int(was_empty)

    def instantiate_record(self):
        record = {
            "metadata": self.folio_client.get_metadata_construct(),
            "id": str(uuid.uuid4()),
            "type": "object",
        }
        self.report_folio_mapping("id", True)
        self.report_folio_mapping("metadata", True)
        return record

    def add_stats(self, a):
        # TODO: Move to interface or parent class
        if a not in self.stats:
            self.stats[a] = 1
        else:
            self.stats[a] += 1

    def validate(self, folio_record, legacy_id, required_fields):
        failures = []
        for req in required_fields:
            if req not in folio_record:
                failures.append(req)
                self.add_to_migration_report(
                    "Failed records that needs to get fixed",
                    f"Required field {req} is missing from {legacy_id}",
                )
        if len(failures) > 0:
            self.add_to_migration_report("User validation", "Total failed users")
            for failure in failures:
                self.add_to_migration_report("Record validation", f"{failure}")
            raise ValueError(f"Record {legacy_id} failed validation {failures}")

    @staticmethod
    def print_dict_to_md_table(my_dict, h1="", h2=""):
        d_sorted = {k: my_dict[k] for k in sorted(my_dict)}
        print(f"{h1} | {h2}")
        print("--- | ---:")
        for k, v in d_sorted.items():
            print(f"{k} | {v}")

    def add_to_migration_report(self, header, measure_to_add):
        if header not in self.migration_report:
            self.migration_report[header] = {}
        if measure_to_add not in self.migration_report[header]:
            self.migration_report[header][measure_to_add] = 1
        else:
            self.migration_report[header][measure_to_add] += 1

    @abstractmethod
    def get_prop(self, legacy_item, folio_prop_name, index_or_id, i=0):
        raise NotImplementedError(
            "This method needs to be implemented in a implementing class"
        )

    def do_map(self, legacy_object, index_or_id):
        folio_object = self.instantiate_record()
        for prop_name, prop in self.schema["properties"].items():
            try:
                if prop.get("description", "") == "Deprecated":
                    self.report_folio_mapping(f"{prop_name} (deprecated)", False, True)
                    # continue
                elif (
                    prop_name in ["metadata", "id", "type"]
                    or prop_name.startswith("effective")
                    or prop.get("folio:isVirtual", False)
                ):
                    continue
                elif prop["type"] == "object":
                    temp_object = {}
                    prop_key = prop_name
                    if "properties" in prop:
                        for sub_prop_name, sub_prop in prop["properties"].items():
                            sub_prop_key = prop_key + "." + sub_prop_name
                            if "properties" in sub_prop:
                                for sub_prop_name2, sub_prop2 in sub_prop[
                                    "properties"
                                ].items():
                                    sub_prop_key2 = sub_prop_key + "." + sub_prop_name2
                                    if sub_prop2["type"] == "array":
                                        print(f"Array: {sub_prop_key2} ")
                            elif sub_prop["type"] == "array":
                                temp_object[sub_prop_name] = []
                                for i in range(0, 5):
                                    if sub_prop["items"]["type"] == "object":
                                        temp = {}
                                        for sub_prop_name2, sub_prop2 in sub_prop[
                                            "items"
                                        ]["properties"].items():
                                            temp[sub_prop_name2] = self.get_prop(
                                                folio_object,
                                                sub_prop_key + "." + sub_prop_name2,
                                                index_or_id,
                                                i,
                                            )
                                        if not all(
                                            value for key, value in temp.items()
                                        ):
                                            self.add_to_migration_report(
                                                "Skipped props since empty",
                                                f"{prop_name}.{sub_prop_name}",
                                            )
                                            continue
                                        temp_object[sub_prop_name].append(temp)
                                    else:
                                        mkey = sub_prop_key + "." + sub_prop_name2
                                        a = self.get_prop(
                                            legacy_object, mkey, index_or_id, i
                                        )
                                        if a:
                                            temp_object[sub_prop_name] = a
                            else:
                                p = self.get_prop(
                                    legacy_object, sub_prop_key, index_or_id
                                )
                                if p:
                                    temp_object[sub_prop_name] = p
                        if temp_object:
                            folio_object[prop_name] = temp_object

                elif prop["type"] == "array":
                    # handle departments
                    if prop["items"]["type"] == "object":
                        self.map_objects_array_props(
                            legacy_object,
                            prop_name,
                            prop["items"]["properties"],
                            folio_object,
                            index_or_id,
                        )
                    elif prop["items"]["type"] == "string":
                        self.map_string_array_props(
                            legacy_object, prop_name, folio_object, index_or_id
                        )
                    else:
                        self.report_folio_mapping(
                            f'Unhandled array of {prop["items"]["type"]}: {prop_name}',
                            False,
                        )
                else:  # Basic property
                    self.map_basic_props(
                        legacy_object, prop_name, folio_object, index_or_id
                    )
            except TransformationDataError as data_error:
                self.add_stats("Data issues found")
                self.error_file.write(data_error)

        del folio_object["type"]
        return folio_object

    def map_objects_array_props(
        self, legacy_object, prop_name, properties, folio_object, index_or_id
    ):
        excluded_props = ["staffOnly"]
        a = []
        for i in range(0, 15):
            temp_object = {}
            for prop in (
                k for k, p in properties.items() if not p.get("folio:isVirtual", False)
            ):
                prop_path = f"{prop_name}[{i}].{prop}"
                res = self.get_prop(legacy_object, prop_path, index_or_id, i)
                self.report_legacy_mapping(self.legacy_property(prop), True, True)
                self.report_folio_mapping(prop, True, False)
                temp_object[prop] = res

            if all(v for k, v in temp_object.items() if k not in excluded_props):
                a.append(temp_object)
        if any(a):
            folio_object[prop_name] = a

    def map_string_array_props(self, legacy_object, prop, folio_object, index_or_id):
        if self.has_property(legacy_object, prop):  # is there a match in the csv?
            mapped_prop = self.get_prop(legacy_object, prop, index_or_id).strip()
            if mapped_prop:
                folio_object.get(prop, []).append(mapped_prop)
                self.report_legacy_mapping(self.legacy_property(prop), True, False)
                self.report_folio_mapping(prop, True, False)
            else:  # Match but empty field. Lets report this
                self.report_legacy_mapping(self.legacy_property(prop), True, True)
                self.report_folio_mapping(prop, True, True)
        else:
            self.report_folio_mapping(prop, False)

    def map_basic_props(self, legacy_object, prop, folio_object, index_or_id):
        if self.has_property(legacy_object, prop):  # is there a match in the csv?
            mapped_prop = self.get_prop(legacy_object, prop, index_or_id).strip()
            if mapped_prop:
                folio_object[prop] = mapped_prop
                self.report_legacy_mapping(self.legacy_property(prop), True, False)
                self.report_folio_mapping(prop, True, False)
            else:  # Match but empty field. Lets report this
                self.report_legacy_mapping(self.legacy_property(prop), True, True)
                self.report_folio_mapping(prop, True, True)
        else:
            self.report_folio_mapping(prop, False)

    def get_objects(self, source_file):
        reader = csv.DictReader(source_file, dialect="tsv")
        for row in reader:
            yield row

    def has_property(self, legacy_object, folio_prop_name):
        arr_re = r"\[[0-9]\]"
        if self.use_map:
            legacy_key = next(
                (
                    k["legacy_field"]
                    for k in self.record_map["data"]
                    if re.sub(arr_re, ".", k["folio_field"]).strip(".")
                    == folio_prop_name
                ),
                "",
            )
            # print(f"{folio_prop_name} - {legacy_key}")
            b = (
                legacy_key
                and legacy_key not in ["", "Not mapped"]
                and legacy_object.get(legacy_key, "")
            )
            return b
        else:
            return folio_prop_name in legacy_object

    def legacy_property(self, folio_prop):
        arr_re = r"\[[0-9]\]"
        if self.use_map:
            return next(
                (
                    k["legacy_field"]
                    for k in self.record_map["data"]
                    if re.sub(arr_re, ".", k["folio_field"]).strip(".") == folio_prop
                ),
                "",
            )
        else:
            return folio_prop

    def get_ref_data_tuple_by_code(self, ref_data, ref_name, code):
        return self.get_ref_data_tuple(ref_data, ref_name, code, "code")

    def get_ref_data_tuple_by_name(self, ref_data, ref_name, name):
        return self.get_ref_data_tuple(ref_data, ref_name, name, "name")

    def get_ref_data_tuple(self, ref_data, ref_name, key_value, key_type):
        dict_key = f"{ref_name}{key_type}"
        ref_object = self.ref_data_dicts.get(dict_key, {}).get(
            key_value.lower().strip(), ()
        )
        if ref_object:
            return ref_object
        else:
            d = {}
            for r in ref_data:
                d[r[key_type].lower()] = (r["id"], r["name"])
            self.ref_data_dicts[dict_key] = d
        return self.ref_data_dicts.get(dict_key, {}).get(key_value.lower().strip(), ())


def as_str(s):
    try:
        return str(s), ""
    except ValueError:
        return "", s
