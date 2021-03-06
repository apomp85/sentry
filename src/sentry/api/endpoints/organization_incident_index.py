from __future__ import absolute_import

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from sentry import features
from sentry.api.bases.organization import (
    OrganizationEndpoint,
    OrganizationPermission,
)
from sentry.api.paginator import OffsetPaginator
from sentry.api.serializers import serialize
from sentry.api.serializers.rest_framework import ListField
from sentry.incidents.logic import create_incident
from sentry.incidents.models import (
    Incident,
    IncidentStatus,
)
from sentry.models.group import Group
from sentry.models.project import Project


class OrganizationIncidentPermission(OrganizationPermission):
    scope_map = {
        'GET': ['org:read', 'org:write', 'org:admin'],
        'POST': ['org:write', 'org:admin'],
    }


class IncidentSerializer(serializers.Serializer):
    projects = ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        default=[],
    )
    groups = ListField(
        child=serializers.CharField(),
        required=True,
        allow_null=False,
        default=[],
    )
    title = serializers.CharField(required=True)
    query = serializers.CharField(required=False)
    dateStarted = serializers.DateTimeField(required=True)
    dateDetected = serializers.DateTimeField(required=False)

    def validate_projects(self, attrs, source):
        slugs = attrs[source]
        projects = Project.objects.filter(
            organization=self.context['organization'],
            slug__in=slugs,
        )
        if len(projects) != len(slugs):
            raise serializers.ValidationError('Invalid project slug(s)')
        attrs[source] = projects
        return attrs

    def validate_groups(self, attrs, source):
        group_ids = attrs[source]
        groups = Group.objects.filter(id__in=group_ids).select_related('project')
        if len(groups) != len(group_ids):
            raise serializers.ValidationError('Invalid group id(s)')
        attrs[source] = groups
        return attrs


class OrganizationIncidentIndexEndpoint(OrganizationEndpoint):
    permission_classes = (OrganizationIncidentPermission, )

    def get(self, request, organization):
        """
        List Incidents that a User can access within an Organization
        ````````````````````````````````````````````````````````````
        Returns a paginated list of Incidents that a user can access.

        :auth: required
        """
        if not features.has('organizations:incidents', organization, actor=request.user):
            return self.respond(status=404)

        incidents = Incident.objects.fetch_for_organization(
            organization,
            self.get_projects(request, organization),
        )

        return self.paginate(
            request,
            queryset=incidents,
            order_by='date_started',
            paginator_cls=OffsetPaginator,
            on_results=lambda x: serialize(x, request.user),
        )

    def post(self, request, organization):
        if not features.has('organizations:incidents', organization, actor=request.user):
            return self.respond(status=404)

        serializer = IncidentSerializer(
            data=request.DATA,
            context={'organization': organization},
        )

        if serializer.is_valid():

            result = serializer.object
            groups = result['groups']
            all_projects = set(result['projects']) | set(g.project for g in result['groups'])
            if any(p for p in all_projects if not request.access.has_project_access(p)):
                raise PermissionDenied

            incident = create_incident(
                organization=organization,
                status=IncidentStatus.CREATED,
                title=result['title'],
                query=result.get('query', ''),
                date_started=result['dateStarted'],
                date_detected=result.get('dateDetected', result['dateStarted']),
                projects=all_projects,
                groups=groups,
            )
            return Response(serialize(incident, request.user), status=201)
        return Response(serializer.errors, status=400)
