# veda-data

This repository houses data and config used to create STAC records to be published to the veda STAC catalog.

## Repository layout

The repo follows the following folder structure:

```text
| ingestion-data
    | collections
        | archive
        . collection-1.json
        . collection-2.json
        ...
        . collection-n.json
    | discovery-items
        | archive
            . archived-discovery-items-1.json
            . archived-discovery-items-2.json
            ...
            . archived-discovery-items-n.json
        . discovery-items-1.json
        . discovery-items-2.json
        ...
        . discovery-items-n.json
| notebooks
```

## ingestion-data

### collections

These are STAC collection records for all the available datasets. They should conform to the [STAC specification for a collection](https://github.com/radiantearth/stac-spec/blob/master/collection-spec/collection-spec.md).

#### archive

These are the collections that we no longer update. However, we might still maintain them in the catalog.

### discovery-items

These are the items ingestion config files that are used by our data pipelines (airflow), specifically the `veda_discover` DAG in [veda-data-airflow](https://github.com/NASA-IMPACT/veda-data-airflow), which discovers all the specified files and triggers the `veda_ingest_raster` DAG which takes care of creating the stac items and publishing them.

The format looks like this:

```json
{
    "collection": "<coll_name>",
    "bucket": "<bucket>",
    "prefix": "<prefix>/",
    "filename_regex": "<file_regex>",
    "id_regex": "<id_regex>",
    "id_template": "<id_template_string>",
    "datetime_range": "<year>|<month>|<day>",
    "assets": {
        "<asset1_name>": {
            "title": "<asset_title>",
            "description": "<asset_description>",
            "regex": "<asset_regex>",
        },
        "<asset2_name>": {
            "title": "<asset_title>",
            "description": "<asset_description>",
            "regex": "<asset_regex>",
        },
    },
}
```

### transfer-config

These are the configs used to transfer assets from the dev bucket (`ghgc-data-store-develop` - where the data was delivered) to the production bucket (`ghgc-data-store` - where the data is moved after it is finalized). The files from the production bucket is used to publish to the catalog. The transfer is done via triggering `veda_transfer` DAG in [veda-data-airflow](https://github.com/NASA-IMPACT/veda-data-airflow).

#### Description of each field

| Field              | Description                                      |
|--------------------|--------------------------------------------------|
| `collection`         | The collection id for the collection that the items need to be ingested to                                                 |
| `bucket`             | The s3 bucket where the item files are located                                                 |
| `prefix`             | The s3 prefix under which to search for the files                                                 |
| `filename_regex`     | The regex pattern that the files to be discovered should match                                                 |
| `id_regex`           | Specifies in regex what part of the filename (usually the datetime) should be used to group assets into item. Example: if the filenames are `asset1_20151201.tif`, `asset2_20151201.tif`, `asset1_20161201.tif`, `asset2_20161201.tif`; the item should be based on the datetime part, hence it'd be `".*_(.*).tif$"`. The part should be specified using round brackets. The is also the part of the filenames that will be used to form the item id, together with the `id_template` field.                                                  |
| `id_template`        | This is a python f-string formatted string that is used to define the `id` of the STAC item. It's used together with the value of `id_regex`. So, going off of the example above, if the `id_template` is `eccodarwin-{}`, then the two item `id`s would be `eccodarwin-20151201` and `eccodarwin-20161201`                                                    |
| `datetime_range`     | This is used to extract the datetime range from the filename. Valid values are `day`, `month` and `year`. Example: if the filename has `20160104` in it, and `datetime_range` is `day` - the `start_datetime` and `end_datetime` are the start and end of the day. For `month`, they are the start and end of the month and so on.                                                   |
| `<asset_name>`       | An `id` for the asset                                   |
| `assets.<asset_name>.title`       | A title for the asset                                   |
| `assets.<asset_name>.description` | A description for the asset                                   |
| `assets.<asset_name>.regex`       | The regex pattern that matches a filename to its respective asset                                   |

#### archive

These are the discovery-items config for collections that we no longer update.

## notebooks

Sometimes, there are exceptional datasets that might require a one-off ingestion that is not supported by the current state of our data pipelines. In such cases, we create notebooks/python scripts that can be used to ingest those data. This is where those notebooks/python scripts live.
