import requests
import xmltodict
import json


def validate_data(xml):
    '''Function that validates data'''
    xml_str = json.dumps(xmltodict.parse(xml))
    xml_json = json.loads(xml_str)
    submission_json = xml_json['data']
    valid = []

    try:
        if type(submission_json['repeat_group'] == list):

            interaction_details = [interaction['capture_insect_details']['insect_scientific_name']
                                   for interaction in submission_json['repeat_group']]

            return interaction_details
    except:
        pass
