import requests
import xmltodict
import json


def check_gbif_data(name):
    '''check whether a name exists in gbif database'''

    url = "https://api.gbif.org/v1/occurrence/search"
    querystring = {"scientificName": "{}".format(name)}
    payload = ""
    response = requests.request("GET", url, data=payload, params=querystring)

    occurences = response.json()

    occurence_list = set([name['species'] for name in occurences['results']])

    return occurence_list


def validate_data(xml):
    '''Function that validates data'''
    xml_str = json.dumps(xmltodict.parse(xml))
    xml_json = json.loads(xml_str)
    submission_json = xml_json['data']
    valid = []

    try:
        if type(submission_json['repeat_group'] == list):

            try:
                insect_name_other = [interaction['capture_insect_details']['insect_scientific_name_other']
                                     for interaction in submission_json['repeat_group']]

                if insect_name_other:
                    for name in set(insect_name_other):

                        if name in check_gbif_data(name):
                            valid.append('T')
                        else:
                            valid.append('F')
            except:
                pass
        else:
            try:
                name = submission_json['capture_insect_details']['insect_scientific_name_other']

                if name in check_gbif_data(name):
                    valid.append('T')
                else:
                    valid.append('F')
            except:
                pass

        return valid

    except:
        pass
