import requests
import xmltodict
import json

def validate_data(xml):
    '''Function that validates data'''
    xml_str = json.dumps(xmltodict.parse(xml))
    xml_json = json.loads(xml_str)
    submission_form = xml_json['icipe']
    insect_name_bool = []

    if 'repeat_group' in submission_form:
        repeat_group = submission_form['repeat_group']
    else:
        repeat_group = []

    if repeat_group:
        repeat_dict = json.dumps(repeat_group)
        repeat_json = json.loads(repeat_dict) #!FIXME Quick hack to get over UnboundLocalError
        
        insect_name = repeat_json['capture_insect_details']['insect_scientific_name_other']
        # if insect_name_list:
        insect_name_bool = []
        # if insect_name_list:
        #     for name in insect_name_list:
        url = "https://api.gbif.org/v1/occurrence/search"
        querystring = {"scientificName":"{}".format(insect_name)}
        payload = ""
        response = requests.request("GET", url, data=payload, params=querystring)

        occurences = response.json()

        occurence_list = set([name['species'] for name in occurences['results']])

        insect_name_bool.append('T') if insect_name in occurence_list else insect_name_bool.append('F')
        # else:
        #     pass

    return False if 'F' in insect_name_bool and len(insect_name_bool) > 0 else True