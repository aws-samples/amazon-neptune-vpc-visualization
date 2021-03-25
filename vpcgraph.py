#!/usr/bin/env python3
import boto3
import urllib3
import os
import tempfile
import uuid
import json
import time

from pprint import pprint

ec2 = boto3.client('ec2')
s3 = boto3.client('s3')
sts = boto3.client('sts')

s3_bucket_name = os.environ['S3Bucket']
neptune_endpoint = os.environ['NeptuneEndpoint']
neptune_iam_role = os.environ['NeptuneIAMRole']
aws_region = os.environ['AWS_REGION']

loader_timeout_seconds = 300  # Max time allowed to load into Neptune


def get_all_igws():
    igws = list()
    next_token = None
    while True:
        if next_token is not None:
            resp = ec2.describe_internet_gateways(
                    NextToken=next_token
            )
        else:
            resp = ec2.describe_internet_gateways()
        igws.extend(resp['InternetGateways'])
        if resp.get('NextToken'):
            next_token = resp['next_token']
        else:
            break

    return igws


def get_all_peering_connections():
    peering_connections = list()
    next_token = None
    while True:
        if next_token is not None:
            resp = ec2.describe_vpc_peering_connections(
                    NextToken=next_token
            )
        else:
            resp = ec2.describe_vpc_peering_connections()
        peering_connections.extend(resp['VpcPeeringConnections'])
        if resp.get('NextToken'):
            next_token = resp['next_token']
        else:
            break

    return peering_connections


def check_loader_status(neptune_url, load_id):
    headers = {
            'Content-Type': 'application/json'
    }

    http = urllib3.PoolManager()

    url = neptune_url + "/" + load_id + "?details=true&errors=true"
    print(url)

    # Check Neptune Loader Status
    response = http.request(
        'GET',
        url,
        headers=headers
    )

    print(response.status)
    print(response.data.decode('utf-8'))

    if response.status != 200:
        raise Exception(response.data.decode('utf-8'))

    response_json = json.loads(response.data.decode('utf-8'))
    status = response_json['payload']['overallStatus']['status']
    print(status)

    if status == "LOAD_FAILED":
        raise Exception("LOAD_FAILED")

    if status == "LOAD_COMPLETED":
        print("Load completed...")
        return True

    return False


def handler(event, context):
    igws = get_all_igws()
    peerings = get_all_peering_connections()

    # Neptune data loading template for Peering connections
    peer_templates = list()
    peer_templates.append(
        "<http://xmlns.com/foaf/0.2/{ReqVpcId}>" +
        " <http://purl.org/dc/elements/PEERS_WITH>" +
        " <http://xmlns.com/foaf/0.2/{VpcId}> .\n")
    peer_templates.append(
        "<http://xmlns.com/foaf/0.2/{ReqVpcId}>" +
        " <http://www.w3.org/2000/01/rdf-schema#label> \"{ReqVpcId}\" .\n")
    peer_templates.append(
        "<http://xmlns.com/foaf/0.2/{VpcId}>" +
        " <http://www.w3.org/2000/01/rdf-schema#label> \"{VpcId}\" .\n")

    # Neptune data loading template for IGW connections
    igw_templates = list()
    igw_templates.append(
        "<http://xmlns.com/foaf/0.2/{VpcId}>" +
        " <http://purl.org/dc/elements/CONNECTS_TO>" +
        " <http://xmlns.com/foaf/0.2/Internet> .\n")
    igw_templates.append(
        "<http://xmlns.com/foaf/0.2/Internet>" +
        " <http://www.w3.org/2000/01/rdf-schema#label> \"Internet\" .\n")
    igw_templates.append(
        "<http://xmlns.com/foaf/0.2/{VpcId}>" +
        " <http://www.w3.org/2000/01/rdf-schema#label> \"{VpcId}\" .\n")

    file = tempfile.NamedTemporaryFile(suffix=".rdf")

    # Print out peering connections
    for peering in peerings:
        acceptor_vpc_id = peering['AccepterVpcInfo']['VpcId']
        requestor_vpc_id = peering['RequesterVpcInfo']['VpcId']

        for peer_template in peer_templates:
            print((peer_template.format(
                VpcId=acceptor_vpc_id, ReqVpcId=requestor_vpc_id)))
            file.write(peer_template.format(
                VpcId=acceptor_vpc_id,
                ReqVpcId=requestor_vpc_id).encode('utf-8'))

    # Print out IGWs
    for igw in igws:
        for attachment in igw['Attachments']:
            if attachment['State'] == 'available':

                for igw_template in igw_templates:
                    print(igw_template.format(VpcId=attachment['VpcId']))
                    file.write(
                        igw_template.format(
                            VpcId=attachment['VpcId']).encode('utf-8'))

    # Load rdf file into S3
    s3_object_key = "{}.rdf".format(str(uuid.uuid4()))
    file.seek(0)
    s3.upload_fileobj(file, s3_bucket_name, s3_object_key)
    print('upload done')

    s3_object_url = "s3://{}/{}".format(s3_bucket_name, s3_object_key)

    headers = {
            'Content-Type': 'application/json'
    }

    http = urllib3.PoolManager()
    data = {
        "source": s3_object_url,
        "format": "ntriples",
        "iamRoleArn": neptune_iam_role,
        "region": aws_region,
        "failOnError": "FALSE",
        "parallelism": "MEDIUM",
        "updateSingleCardinalityProperties": "FALSE",
        "queueRequest": "TRUE"
    }
    url = "https://" + neptune_endpoint + "/loader"

    pprint(data)

    # Invoke Neptune Loader
    response = http.request(
        'POST',
        url,
        headers=headers,
        body=json.dumps(data).encode('utf-8')
    )

    print(response.status)
    print(response.data.decode('utf-8'))

    if response.status != 200:
        raise Exception(response.data.decode('utf-8'))

    response_json = json.loads(response.data.decode('utf-8'))

    timeout = time.time() + loader_timeout_seconds
    done = False
    while not done:
        time.sleep(15)
        print("Checking loader status")
        done = check_loader_status(url, response_json['payload']['loadId'])

        if not done and time.time() > timeout:
            raise Exception("Timed out waiting for Neptune loader to complete")

    return "OK"
