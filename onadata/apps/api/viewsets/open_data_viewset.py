import json
import re
from collections import OrderedDict

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext as _
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from onadata.apps.api.permissions import OpenDataViewSetPermissions
from onadata.apps.api.tools import get_baseviewset_class
from onadata.apps.logger.models import Instance
from onadata.apps.logger.models.open_data import OpenData
from onadata.apps.logger.models.xform import XForm, question_types_to_exclude
from onadata.apps.viewer.models.data_dictionary import DataDictionary
from onadata.libs.data import parse_int
from onadata.libs.utils.logger_tools import remove_metadata_fields
from onadata.libs.mixins.cache_control_mixin import CacheControlMixin
from onadata.libs.mixins.etags_mixin import ETagsMixin
from onadata.libs.pagination import StandardPageNumberPagination
from onadata.libs.serializers.data_serializer import TableauDataSerializer
from onadata.libs.serializers.open_data_serializer import OpenDataSerializer
from onadata.libs.utils.common_tools import json_stream

from onadata.libs.utils.common_tags import (DURATION, ID, ATTACHMENTS,
                                            MEDIA_ALL_RECEIVED, MEDIA_COUNT,
                                            NOTES, SUBMISSION_TIME, NA_REP,
                                            SUBMITTED_BY, TAGS, TOTAL_MEDIA,
                                            UUID, VERSION, REVIEW_STATUS,
                                            REVIEW_COMMENT, REPEAT_SELECT_TYPE,
                                            MULTIPLE_SELECT_TYPE)

from onadata.libs.utils.cache_tools import TABLEAU_COLUMN_HEADERS

BaseViewset = get_baseviewset_class()
IGNORED_FIELD_TYPES = ['select one', 'select multiple']
GPS_DATA = ['geopoint', 'gps']

# index tags
DEFAULT_OPEN_TAG = '['
DEFAULT_CLOSE_TAG = ']'
DEFAULT_INDEX_TAGS = (DEFAULT_OPEN_TAG, DEFAULT_CLOSE_TAG)
DEFAULT_NA_REP = getattr(settings, 'NA_REP', NA_REP)


def replace_special_characters_with_underscores(data):
    return [re.sub(r"\W", r"_", a) for a in data]


def process_tableau_data(data, xform):
    """
    Streamlines the row header fields
    with the column header fields for the same form.
    Handles Flattenning repeat data for tableau
    """
    def get_xpath(key, nested_key):
        val = nested_key.split('/')
        nested_key_diff = val[len(key.split('/')):]
        xpaths = key + f'[{index}]/' + '/'.join(nested_key_diff)
        return xpaths

    def get_ordered_repeat_value(key, item, index):
        """
        Return Ordered Dict of repeats in the order in which they appear in
        the XForm.
        """
        children = xform.get_child_elements(
            key, split_select_multiples=False)
        item_list = OrderedDict()
        data = {}

        for elem in children:
            if not question_types_to_exclude(elem.type):
                new_xpath = elem.get_abbreviated_xpath()
                item_list[new_xpath] = item.get(new_xpath, DEFAULT_NA_REP)
                # Loop through repeat data and flatten it
                # given the key "children/details" and nested_key/
                # abbreviated xpath
                # "children/details/immunization/polio_1",
                # generate ["children", index, "immunization/polio_1"]
                for (nested_key, nested_val) in item_list.items():
                    qstn_type = xform.get_element(nested_key).type
                    xpaths = get_xpath(key, nested_key)
                    data = get_updated_data_dict(
                        qstn_type, xpaths, nested_val, data)
        return data

    def get_updated_data_dict(qstn_type, key, value, data_dict):
        """
        Unpacks row data in accordance
        with various question types
        """
        if qstn_type == MULTIPLE_SELECT_TYPE:
            choices = value.split(" ")
            for choice in choices:
                xpaths = f'{key}/{choice}'
                data_dict[xpaths] = choice
        elif qstn_type == REPEAT_SELECT_TYPE:
            try:
                for item in value:
                    for (nested_key, nested_val) in item.items():
                        xpath = get_xpath(key, nested_key)
                        data_dict[xpath] = nested_val
            except AttributeError:
                data_dict[key] = value
        elif qstn_type in GPS_DATA:
            parts = value.split(' ')
            gps_xpaths = \
                DataDictionary.get_additional_geopoint_xpaths(
                    key)
            gps_parts = dict(
                [(xpath, None) for xpath in gps_xpaths])
            if len(parts) == 4:
                gps_parts = dict(zip(gps_xpaths, parts))
                data_dict.update(gps_parts)
        else:
            data_dict[key] = value

        return data_dict

    result = []
    if data:
        headers = cache.get(f'{TABLEAU_COLUMN_HEADERS}{xform.pk}')
        tableau_headers = remove_metadata_fields(headers)
        for row in data:
            diff = set(tableau_headers).difference(set(row))
            flat_dict = dict.fromkeys(diff, None)
            for (key, value) in row.items():
                if isinstance(value, list) and key not in [
                        ATTACHMENTS, NOTES]:
                    for index, item in enumerate(value, start=1):
                        # order repeat according to xform order
                        item = get_ordered_repeat_value(key, item, index)
                        flat_dict.update(item)
                else:
                    try:
                        qstn_type = xform.get_element(key).type
                        flat_dict = get_updated_data_dict(
                            qstn_type, key, value, flat_dict)
                    except AttributeError:
                        flat_dict[key] = value

            result.append(flat_dict)
    return result


def fetch_form_submissions(xform, gt_id=None, count_check=False):
    """
    Fetch submissions for Tableau
    """
    if xform.is_merged_dataset:
        qs_kwargs = {'xform_id__in': list(
            xform.mergedxform.xforms.values_list('pk', flat=True))}
    else:
        qs_kwargs = {'xform_id': xform.pk}
    if gt_id:
        qs_kwargs.update({'id__gt': gt_id})
    instances = Instance.objects.filter(
        **qs_kwargs, deleted_at__isnull=True).order_by('pk')

    if count_check:
        xform_json = json.loads(xform.json)
        fields = xform_json.get('children')
        # Capturing fields of type repeat at zero level on the form
        repeat_fields = [
            d['name'] for d in fields if d['type'] == 'repeat']

        # Figure a way to get field path for nested repeat
        # as the name field cannot resonate on the submission data fields

        # for d in fields:
        #     try:
        #         if d['children']:
        #             for x in d['children']:
        #                 if x['type']  == 'repeat':
        #                     repeat_fields.append(x['name'])
        #     except (KeyError, NameError):
        #         continue
        repeat_subs_len = []
        for field in repeat_fields:
            for instance in instances:
                val = len(instance.json[field])
                repeat_subs_len.append({
                    f'field_count': val,
                    f'instance_id': instance.pk,
                    f'repeat_field': field})

        # This is an asumption also that there
        # is only 1 repeat group defined on the xform.
        # Another assumption is that the repeat count
        # at zero level of the form is the
        # same as the nested repeat count
        largest_instance = max(
            repeat_subs_len, key=lambda x: x['field_count'])
        largest_repeat_count = largest_instance['field_count']

        return largest_repeat_count

    return instances


def _get_tableau_headers(xform, repeat_iters):
    """
    Return a list of headers for tableau.
    """
    def shorten(xpath):
        xpath_list = xpath.split('/')
        return '/'.join(xpath_list[2:])

    header_list = [
        shorten(xpath) for xpath in xform.xpaths(
            repeat_iterations=repeat_iters)]
    header_list += [
        ID, UUID, SUBMISSION_TIME, TAGS, NOTES, REVIEW_STATUS,
        REVIEW_COMMENT, VERSION, DURATION, SUBMITTED_BY, TOTAL_MEDIA,
        MEDIA_COUNT, MEDIA_ALL_RECEIVED
    ]
    return header_list


class OpenDataViewSet(ETagsMixin, CacheControlMixin,
                      BaseViewset, ModelViewSet):
    permission_classes = (OpenDataViewSetPermissions, )
    queryset = OpenData.objects.filter()
    lookup_field = 'uuid'
    serializer_class = OpenDataSerializer
    flattened_dict = {}
    MAX_INSTANCES_PER_REQUEST = 1000
    pagination_class = StandardPageNumberPagination

    def get_tableau_type(self, xform_type):
        '''
        Returns a tableau-supported type based on a xform type.
        '''
        tableau_types = {
            'integer': 'int',
            'calculate': 'int',
            'decimal': 'float',
            'dateTime': 'datetime',
            'text': 'string'
        }

        return tableau_types.get(xform_type, 'string')

    def flatten_xform_columns(self, json_of_columns_fields):
        '''
        Flattens a json of column fields and the result is set to a class
        variable.
        '''
        for a in json_of_columns_fields:
            self.flattened_dict[a.get('name')] = self.get_tableau_type(
                a.get('type'))
            if a.get('children'):
                self.flatten_xform_columns(a.get('children'))

    def get_tableau_column_headers(self):
        '''
        Retrieve columns headers that are valid in tableau.
        '''
        tableau_colulmn_headers = []

        def append_to_tableau_colulmn_headers(header, question_type=None):
            quest_type = 'string'
            if question_type:
                quest_type = question_type

            # alias can be updated in the future to question labels
            tableau_colulmn_headers.append({
                'id': header,
                'dataType': quest_type,
                'alias': header
            })
        # Remove metadata fields from the column headers
        # Calling set to remove duplicates in group data
        xform_headers = set(remove_metadata_fields(self.xform_headers))

        # using nested loops to determine what valid data types to set for
        # tableau.
        for header in xform_headers:
            for quest_name, quest_type in self.flattened_dict.items():
                if header == quest_name or header.endswith('_%s' % quest_name):
                    append_to_tableau_colulmn_headers(header, quest_type)
                    break
            else:
                if header == '_id':
                    append_to_tableau_colulmn_headers(header, "int")
                else:
                    append_to_tableau_colulmn_headers(header)

        return tableau_colulmn_headers

    @action(methods=['GET'], detail=True)
    def data(self, request, **kwargs):
        """
        Streams submission data response matching uuid in the request.
        """
        self.object = self.get_object()
        # get greater than value and cast it to an int
        gt_id = request.query_params.get('gt_id')
        gt_id = gt_id and parse_int(gt_id)
        count = request.query_params.get('count')
        pagination_keys = [
            self.paginator.page_query_param,
            self.paginator.page_size_query_param
        ]
        query_param_keys = request.query_params
        should_paginate = any([k in query_param_keys for k in pagination_keys])

        data = []
        if isinstance(self.object.content_object, XForm):
            if not self.object.active:
                return Response(status=status.HTTP_404_NOT_FOUND)

            xform = self.object.content_object
            instances = fetch_form_submissions(
                            xform, gt_id)
            if count:
                return Response({'count': instances.count()})

            if should_paginate:
                instances = self.paginate_queryset(instances)

            data = process_tableau_data(
                TableauDataSerializer(instances, many=True).data, xform)

            return self._get_streaming_response(data)

        return Response(data)

    def _get_streaming_response(self, data):
        """Get a StreamingHttpResponse response object"""

        def get_json_string(item):
            return json.dumps({
                re.sub(r"\W", r"_", a): b for a, b in item.items()})

        response = StreamingHttpResponse(
            json_stream(data, get_json_string),
            content_type="application/json"
        )

        # set headers on streaming response
        for k, v in self.headers.items():
            response[k] = v

        return response

    def destroy(self, request, *args, **kwargs):
        self.get_object().delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(methods=['GET'], detail=True)
    def schema(self, request, **kwargs):
        self.object = self.get_object()
        if isinstance(self.object.content_object, XForm):
            xform = self.object.content_object
            sub_with_largest_repeat = fetch_form_submissions(
                                        xform, count_check=True)

            headers = _get_tableau_headers(xform, sub_with_largest_repeat)
            self.xform_headers = replace_special_characters_with_underscores(
                headers)
            cache.set(
                f'{TABLEAU_COLUMN_HEADERS}{xform.pk}', self.xform_headers)
            tableau_column_headers = self.get_tableau_column_headers()

            data = {
                'column_headers': tableau_column_headers,
                'connection_name': "%s_%s" % (xform.project_id,
                                              xform.id_string),
                'table_alias': xform.title
            }

            return Response(data=data, status=status.HTTP_200_OK)

        return Response(status=status.HTTP_404_NOT_FOUND)

    @action(methods=['GET'], detail=False)
    def uuid(self, request, *args, **kwargs):
        data_type = request.query_params.get('data_type')
        object_id = request.query_params.get('object_id')

        if not data_type or not object_id:
            return Response(
                data="Query params data_type and object_id are required",
                status=status.HTTP_400_BAD_REQUEST)

        if data_type == 'xform':
            xform = get_object_or_404(XForm, id=object_id)
            if request.user.has_perm("change_xform", xform):
                ct = ContentType.objects.get_for_model(xform)
                _open_data = get_object_or_404(
                    OpenData, object_id=object_id, content_type=ct)
                if _open_data:
                    return Response(
                        data={'uuid': _open_data.uuid},
                        status=status.HTTP_200_OK)
            else:
                raise PermissionDenied(
                    _((u"You do not have permission to perform this action.")))

        return Response(status=status.HTTP_404_NOT_FOUND)
