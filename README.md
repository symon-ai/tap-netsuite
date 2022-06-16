# tap-netsuite

[Singer](https://www.singer.io/) tap that extracts data from a [NetSuite](https://www.netsuite.com/) database and produces JSON-formatted data following the [Singer spec](https://github.com/singer-io/getting-started/blob/master/SPEC.md).

# How to use it
tap-netsuite works together with any other Singer Target to move data from NetSuite to any target destination.


## Install and run

First, make sure Python 3 is installed on your system or follow these
installation instructions for [Mac](http://docs.python-guide.org/en/latest/starting/install3/osx/) or
[Ubuntu](https://www.digitalocean.com/community/tutorials/how-to-install-python-3-and-set-up-a-local-programming-environment-on-ubuntu-16-04).

It's recommended to use a virtualenv:

```bash
 python3 -m venv ~/.virtualenvs/tap-netsuite/
 source ~/.virtualenvs/tap-netsuite/bin/activate
 pip install -upgrade pip
 pip install -e .
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

## Run Tap:

Run the tap like any other singer compatible tap:

```
$ tap-netsuite --config config.json --properties properties.json
```

## License

Apache License Version 2.0

See [LICENSE](LICENSE) to see the full text.
