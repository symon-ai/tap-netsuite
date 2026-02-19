# tap-netsuite

[Singer](https://www.singer.io/) tap that extracts data from a [NetSuite](https://www.netsuite.com/) database and produces JSON-formatted data following the [Singer spec](https://github.com/singer-io/getting-started/blob/master/SPEC.md).

# How to use it
tap-netsuite works together with any other Singer Target to move data from NetSuite to any target destination.


## Install and run

Ensure poetry is installed on your machine. 

- This command will return the installed version of poetry if it is installed.
```
poetry --version
```

- If not, install poetry using the following commands (from https://python-poetry.org/docs/#installation):
```
curl -sSL https://install.python-poetry.org | python3 -
PATH=~/.local/bin:$PATH
```

Within the `tap-netsuite` directory, install dependencies:
```
poetry install
```

Then run the tap:
```
poetry run tap-netsuite <options>
```


## Configuration
#### Token Based Authentication

Here is an example of basic config, and a bit of a run down on each of the properties:
```
{
  "ns_account":"netsuite_account_id",
  "ns_consumer_key":"netsuite_consumer_key",
  "ns_consumer_secret":"netsuite_consumer_secret",
  "ns_token_key":"netsuite_token_key",
  "ns_token_secret" :"netsuite_token_secret",
  "select_fields_by_default": true,
  "is_sandbox": true / false,
  "start_date": "2019-09-02T00:00:00Z"
}
```
- **ns_account**(_required_): The NetSuite account id. his can be found under Setup -> Company -> Company Information. Look for Account Id. Note "_SB" is for the Sandbox account.


- **ns_consumer_key**(_required_): The consumer key for the integration. This can be found while creating new integration under Setup -> Integrations -> Manage Integrations -> New (Please save it while creating integration as it’s available only for the first time).


- **ns_consumer_secret**(_required_): The consumer secret for the integration. This can be found while creating new integration under Setup -> Integrations -> Manage Integrations -> New (Please save it while creating integration as it’s available only for the first time).


- **ns_token_key**(_required_): The token key found while creating a new token under Setup -> Users/Roles -> Access Tokens -> New (Please save it while creating a token as it’s available only for the first time).


- **ns_token_secret**(_required_): The token secret found while creating a new token under Setup -> Users/Roles -> Access Tokens -> New (Please save it while creating a token as it’s available only for the first time).


- **select_fields_by_default**(_required_): Describes whether or not the tap will select fields by default when new fields are discovered in NetSuite objects.


- **is_sandbox**(_optional_): Should always be set to true if you are connecting the Production account of NetSuite. Set it to false if you want to connect to your SandBox account. Default is false.


- **start_date**(_optional_): Used by the tap as a bound on SOAP requests when searching for records. This should be an RFC3339 formatted date-time, like "2018-01-08T00:00:00Z".


## Discovery mode:

The tap can be invoked in discovery mode to find the available tables and
columns in the database:

```bash
$ tap-netsuite --config config.json --discover > properties.json
```

A discovered catalog is output, with a JSON-schema description of each table. A
source table directly corresponds to a Singer stream.

Edit the `properties.json` and select the streams to replicate. Or use this helpful [discovery utility](https://github.com/chrisgoddard/singer-discover).

### NOTE:
Our firewall results in us having a self-signed certificate in the chain
uncomment the workaround in tap_netsuite/__init__.py if you need to run tap from your terminal

## Run Tap:

Run the tap like any other singer compatible tap:

```
$ tap-netsuite --config config.json --properties properties.json
```

## Package manager
We only use poetry to manage our packages. Pipfile is there because our code scan doesn't support poetry.lock. So we do the following hack to generate Pipfile and Pipfile.lock based on our poetry.lock:
# 1. Export all dependencies from poetry.lock to requirements.txt
```
poetry export -f requirements.txt --output requirements.txt --without-hashes
```
# 1b. (Optional) Make sure pipenv has the right python version
Check:
```
pipenv --support
```
Install:
```
python -m pip install --user pipenv
```

# 2. Generate Pipfile and Pipfile.lock from requirements.txt (make sure you pass in right version of python)
```
pipenv install --python 3.13 -r requirements.txt
```

Check that the required python version in the Pipfile matches your expected python version. For some reason even if requirements.txt specify the right python version pipenv can still default to a different version based on the some stale versioning in the venv. In which case, do the following:

# 1. Delete the Pipfile and lock, and deactivate your venv

# 2. Delete the venv you created manually or with `pipenv --rm`

# 3. Re-run the pipenv install command

## License

Apache License Version 2.0

See [LICENSE](LICENSE) to see the full text.
