"""
Routes of the gn_permissions blueprint
"""

from copy import copy
import datetime
import json
import locale
import random

from flask import (
    Blueprint,
    request,
    Response,
    render_template,
    session,
    current_app,
    url_for,
    redirect,
    jsonify
)
from utils_flask_sqla.response import json_resp
from pypnusershub.db.models import User
from sqlalchemy.orm.exc import NoResultFound

from geonature.utils.env import DB
from geonature.utils.utilsmails import send_mail
from geonature.core.gn_commons.models import TModules
from geonature.core.ref_geo.models import LAreas, BibAreasTypes
from geonature.core.taxonomie.models import Taxref
from geonature.core.gn_permissions.models import (
    BibFiltersType,
    CorObjectModule,
    CorRequestsPermissions,
    CorRoleActionFilterModuleObject,
    TActions,
    TFilters,
    TObjects,
    TRequests,
)
from geonature.core.gn_permissions import decorators as permissions
from geonature.core.gn_permissions.tools import cruved_scope_for_user_in_module


routes = Blueprint("gn_permissions", __name__, template_folder="templates")


@routes.route("/cruved", methods=["GET"])
@permissions.check_cruved_scope("R", True)
@json_resp
def get_cruved(info_role):
    """
    Get the cruved for a user

    .. :quickref: Permissions;

    Params:
    :param user: the user who ask the route, auto kwargs via @check_cruved_scope
    :type user: User
    :param module_code: the code of the requested module - as querystring
    :type module_code: str

    :returns: dict of the CRUVED
    """
    params = request.args.to_dict()

    # get modules
    q = DB.session.query(TModules)
    if "module_code" in params:
        q = q.filter(TModules.module_code.in_(params["module_code"]))
    modules = q.all()

    # for each modules get its cruved
    # then get its related object and their cruved
    modules_with_cruved = {}
    for mod in modules:
        mod_as_dict = mod.as_dict()
        # get mod objects
        module_objects = (
            DB.session.query(TObjects)
            .join(CorObjectModule, CorObjectModule.id_object == TObjects.id_object)
            .filter_by(id_module=mod_as_dict["id_module"])
            .all()
        )

        module_cruved, herited = cruved_scope_for_user_in_module(
            id_role=info_role.id_role, module_code=mod_as_dict["module_code"]
        )
        mod_as_dict["cruved"] = module_cruved

        module_objects_as_dict = {}
        # get cruved for each object
        for _object in module_objects:
            object_as_dict = _object.as_dict()
            object_cruved, herited = cruved_scope_for_user_in_module(
                id_role=info_role.id_role,
                module_code=mod_as_dict["module_code"],
                object_code=_object.code_object,
            )
            object_as_dict["cruved"] = object_cruved
            module_objects_as_dict[object_as_dict["code_object"]] = object_as_dict

            mod_as_dict["module_objects"] = module_objects_as_dict

        modules_with_cruved[mod_as_dict["module_code"]] = mod_as_dict

    return modules_with_cruved


@routes.route("/logout_cruved", methods=["GET"])
def logout():
    """
    Route to logout with cruved

    .. :quickref: Permissions;

    To avoid multiples server call, we store the cruved in the session
    when the user logout we need clear the session to get the new cruved session
    """
    copy_session_key = copy(session)
    for key in copy_session_key:
        session.pop(key)
    return Response("Logout", 200)


@routes.route("/access_requests", methods=["POST"])
@permissions.check_cruved_scope(action='R', get_role=True)
@json_resp
def post_access_request(info_role):
    """
    Post an access request.

    .. :quickref: Permissions;
    """
    # Check if permissions management is enable
    if not current_app.config["PERMISSION_MANAGEMENT"]["ENABLE_ACCESS_REQUEST"]:
        response = {
            "message": "Demande de permissions d'accès non activé sur cette instance de Geonature.",
            "status": "warning"
        }
        return response, 403

    # Transform received data
    data = dict(request.get_json())
    user_id = info_role.id_role

    # Prepare TRequests
    trequest = TRequests(**{
        "id_role": user_id,
        "end_date": format_end_access_date(data),
        "additional_data": format_additional_data(data),
    })

    # Prepare permissions link to TRequests
    if (len(data["areas"]) > 0):
        permission = get_geographic_permission()
        permission.value_filter = build_value_filter_from_list(data["areas"])
        trequest.cor_permissions.append(permission)

    if (len(data["taxa"]) > 0):
        permission = get_taxonomic_permission()
        permission.value_filter = build_value_filter_from_list(data["taxa"])
        trequest.cor_permissions.append(permission)

    if (data["sensitive_access"] is True):
        permission = get_sensitivity_permission()
        permission.value_filter = "true"
        trequest.cor_permissions.append(permission)

    # Write request_data in database
    DB.session.add(trequest)
    DB.session.commit()

    # Inform about new access request by email
    send_email_after_access_request(
        data=data,
        user_id=user_id,
        request_token=trequest.token,
    )

    #return request.as_dict(True)
    response = {
        "message": "Succès de l'ajout de la demande d'accès.",
        "status": "success"
    }
    return response, 200


def format_end_access_date(data, date_format="%Y-%m-%d"):
    formated_end_date = None
    end_date = data["end_access_date"]
    if (end_date):
        # TODO : see how to define locale globaly => bug with python 3.7?
        print(f"Current locale: {locale.getlocale(locale.LC_TIME)}")
        locale.setlocale(locale.LC_TIME, "fr_FR.UTF-8")
        date = datetime.date(end_date["year"], end_date["month"], end_date["day"])
        formated_end_date = date.strftime(date_format)
    return formated_end_date


def format_additional_data(data):
    raw_data = data.copy()
    raw_data.pop("additional_data", None)
    data["additional_data"]["originalRawData"] = raw_data
    return data["additional_data"]


def build_value_filter_from_list(data):
    unduplicated_data = []
    [unduplicated_data.append(x) for x in data if x not in unduplicated_data]
    return ",".join(map(str, unduplicated_data))


def get_geographic_permission():
    return get_fresh_permission(filter_type_code="GEOGRAPHIC")


def get_taxonomic_permission():
    return get_fresh_permission(filter_type_code="TAXONOMIC")


def get_sensitivity_permission():
    return get_fresh_permission(filter_type_code="SENSITIVITY", object_code="SENSITIVE_OBSERVATION")


def get_fresh_permission(
    filter_type_code,
    module_code="SYNTHESE",
    action_code="R",
    object_code="PRIVATE_OBSERVATION"
):
    permission_module = DB.session.query(TModules).filter(TModules.module_code == module_code).one()
    permission_action = DB.session.query(TActions).filter(TActions.code_action == action_code).one()
    permission_object = DB.session.query(TObjects).filter(TObjects.code_object == object_code).one()
    permission_filter_type = (DB.session
        .query(BibFiltersType)
        .filter(BibFiltersType.code_filter_type == filter_type_code)
        .one()
    )
    return CorRequestsPermissions(
        id_module=permission_module.id_module,
        id_action=permission_action.id_action,
        id_object=permission_object.id_object,
        id_filter_type=permission_filter_type.id_filter_type,
    )


def send_email_after_access_request(data, user_id, request_token):
    recipients = current_app.config["PERMISSION_MANAGEMENT"]["VALIDATOR_EMAIL"]
    app_name = current_app.config["appName"]
    subject = f"Demande de permissions d'accès {app_name}"
    msg_html = render_request_approval_tpl(user_id, data, request_token)
    send_mail(recipients, subject, msg_html)


def render_request_approval_tpl(user_id, data, request_token):
    template = "email_admin_request_approval.html"
    approval_url = url_for(
        "gn_permissions.manage_access_request_by_link",
        token=request_token,
        action="approve",
    )
    refusal_url = url_for(
        "gn_permissions.manage_access_request_by_link",
        token=request_token,
        action="refuse",
    )
    return render_template(
        template,
        app_name=current_app.config["appName"],
        end_date=format_end_access_date(data, date_format="%x"),
        user=get_user_infos(user_id),
        geographic_filter_values=format_geographic_filter_values(data),
        taxonomic_filter_values=format_taxonomic_filter_values(data),
        sensitive_permission=data["sensitive_access"],
        additional_fields=format_additional_fields(data),
        approval_url=approval_url,
        refusal_url=refusal_url,
        app_url = current_app.config["URL_APPLICATION"] + "/#/permissions/requests/pending",
    )


def get_user_infos(user_id):
    user = (DB
        .session.query(User)
        .filter(User.id_role == user_id)
        .first()
        .as_dict()
    )
    print(user)
    return user


def format_geographic_filter_values(data):
    formated_geo = []
    if len(data["areas"]) > 0:
        for area in get_areas_infos(data["areas"]):
            print(f"Area: {area}")
            name = area["area_name"]
            code = area["area_code"]
            if area["type_code"] == "DEP":
                name = f"{name} [{code}]"
            elif area["type_code"] == "COM":
                name = f"{name} [{code[:2]}]"
            formated_geo.append(name)
    return formated_geo


def get_areas_infos(area_ids):
    data = (DB
        .session.query(
            LAreas.area_name,
            LAreas.area_code,
            BibAreasTypes.type_code
        )
        .join(LAreas, LAreas.id_type == BibAreasTypes.id_type)
        .filter(LAreas.id_area.in_(tuple(area_ids)))
        .all()
    )
    return [row._asdict() for row in data]


def format_taxonomic_filter_values(data):
    formated_taxonomic = []
    if len(data["taxa"]) > 0:
        for taxon in get_taxons_infos(data["taxa"]):
            name = taxon["nom_complet_html"]
            code = taxon["cd_nom"]
            formated_taxonomic.append(f"{name} [{code}]")
    return formated_taxonomic


def get_taxons_infos(taxon_ids):
    data = (DB
        .session.query(Taxref)
        .filter(Taxref.cd_nom.in_(tuple(taxon_ids)))
        .all()
    )
    return [row.as_dict() for row in data]


def format_additional_fields(data):
    if data["additional_data"] is None:
        return []

    attr_labels = build_dynamic_request_form_labels()
    attr_labels_keys = attr_labels.keys()
    formated_fields = []
    for key, value in (data.get("additional_data") or {}).items():
        if key in attr_labels_keys:
            formated_fields.append({
                "key": key,
                "label": attr_labels.get(key),
                "value": value,
            })
    return formated_fields


def build_dynamic_request_form_labels():
    attr_labels = {}
    form_cfg = current_app.config["PERMISSION_MANAGEMENT"]["REQUEST_FORM"]
    for cfg in form_cfg:
        if all(key in cfg for key in ("attribut_name", "attribut_label")):
            attr_labels[cfg["attribut_name"]] = cfg["attribut_label"]
    return attr_labels


@routes.route("/access_requests/<token>/<action>", methods=["GET"])
def manage_access_request_by_link(token, action):
    """
        Approuve/Refuse une demande de permissions d'accès.
        ATTENTION : ce webservice modifie une demande d'accès via la
        méthode HTTP GET.
        Dénormalisation permettant de lancer ces actions via un lien
        envoyé par email.
    """
    # Check if permission management is enable on this GeoNature instance
    if not current_app.config["PERMISSION_MANAGEMENT"]["ENABLE_ACCESS_REQUEST"]:
        response = {
            "message": "Demande de permissions d'accès non activé sur cette instance de Geonature.",
            "status": "warning"
        }
        return response, 403

    # Check "action" URL parameter values
    accepted_actions = ["approve", "refuse"]
    if action not in accepted_actions:
        accepted_values_msg = f"Valeurs acceptées : {accepted_actions.join(', ')}."
        response = {
            "message": f"Type d'action '{action}' inconnu. {accepted_values_msg}",
            "status": "error"
        }
        return response, 400

    # Check access request token was defined
    if token is None:
        response = {
            "message": "Token de demande de permission non défini.",
            "status": "error"
        }
        return response, 404

    # Check access request token exists in DB
    request = get_request_by_token(token)
    if not request:
        response = {
            "message": "Token de demande de permission introuvable.",
            "status": "error"
        }
        return response, 404

    # Check if access request was not already approved or refused
    if request["accepted_date"]:
        date = request['accepted_date']
        status = 'acceptée' if request['accepted'] else 'refusée'
        msg = f"Demande de permission déjà {status} le {date}.",
        response = {"message": msg, "status": "error"}
        return response, 400

    # Update access request
    accepted = (True if action == "approve" else False)
    result = (DB.session
        .query(TRequests)
        .filter(TRequests.token == token)
        .update({
            TRequests.accepted: accepted,
            TRequests.accepted_date: datetime.datetime.now().date().strftime("%Y-%m-%d %H:%M:%S"),
        })
    )

    # Copy asked permissions to authorized permissions table if request approve
    result = (DB.session
        .query(BibFiltersType.label_filter_type, CorRequestsPermissions)
        .join(TRequests, CorRequestsPermissions.id_request == TRequests.id_request)
        .join(BibFiltersType, CorRequestsPermissions.id_filter_type == BibFiltersType.id_filter_type)
        .filter(TRequests.token == token)
        .all()
    )
    data = [row._asdict() for row in result]

    for d in data:
        rp = d["CorRequestsPermissions"].as_dict()
        permission = CorRoleActionFilterModuleObject(**{
            "id_role": request["id_role"],
            "id_action": rp["id_action"],
            #"id_filter_type": rp["id_filter_type"], # For next gn_permissions version
            "id_module": rp["id_module"],
            "id_object": rp["id_object"],
            #"value_filter": rp["value_filter"], # For next gn_permissions version
        })

        if not permission.is_permission_already_exist(
            id_role=request["id_role"],
            id_action=rp["id_action"],
            id_module=rp["id_module"],
            id_filter_type=rp["id_filter_type"],
            value_filter=rp["value_filter"],
            id_object=rp["id_object"],
        ):
            permission_filter = get_filter(
                id_filter_type=rp["id_filter_type"],
                value_filter=rp["value_filter"]
            )
            if not permission_filter:
                permission_filter = TFilters(
                    label_filter=f"{d['label_filter_type']} : {rp['value_filter']}",
                    id_filter_type=rp["id_filter_type"],
                    value_filter=rp["value_filter"],
                )
            permission.filter.append(permission_filter)

            DB.session.add(permission)

    # Commit DB session
    DB.session.commit()

    # Send email to user
    send_email_after_managing_request(
        action,
        user_id=request["id_role"],
        data=request["additional_data"]["originalRawData"],
    )

    # Redirect to GeoNature app home page
    return redirect(current_app.config["URL_APPLICATION"], code=302)


def get_request_by_token(token):
    try:
        data = (DB
            .session.query(TRequests)
            .filter(TRequests.token == token)
            .one()
            .as_dict()
        )
    except NoResultFound:
        return False
    print(f"In get_request_by_token(): {data}")
    return data


def get_filter(id_filter_type, value_filter):
    try:
        permission_filter = (DB
            .session.query(TFilters)
            .filter(TFilters.id_filter_type == id_filter_type)
            .filter(TFilters.value_filter == value_filter)
            .one()
        )
    except NoResultFound:
        return False
    return permission_filter


def send_email_after_managing_request(action, user_id, data=None, refuse_reason=None):
    user = get_user_infos(user_id)
    recipient = user['email']
    app_name = current_app.config["appName"]
    if action == "approve":
        subject = f"Acceptation de demande de permissions d'accès {app_name}"
        msg_html = render_approved_request_tpl(user, data)
    else:
        subject = f"Refus de demande de permissions d'accès {app_name}"
        msg_html = render_refused_request_tpl(user, refuse_reason)
    send_mail(recipient, subject, msg_html)


def render_approved_request_tpl(user, data):
    return render_template(
        "email_user_request_approved.html",
        app_name=current_app.config["appName"],
        user=user,
        sensitive_permission=data["sensitive_access"],
        geographic_filter_values=format_geographic_filter_values(data),
        taxonomic_filter_values=format_taxonomic_filter_values(data),
        end_date=format_end_access_date(data, date_format="%x"),
        app_url=current_app.config["URL_APPLICATION"],
        validators=get_validators(),
    )


def render_refused_request_tpl(user, refuse_reason=None):
    return render_template(
        "email_user_request_refused.html",
        app_name=current_app.config["appName"],
        user=user,
        refuse_reason=refuse_reason,
        validators=get_validators(),
    )


def get_validators():
    validators = current_app.config["PERMISSION_MANAGEMENT"]["VALIDATOR_EMAIL"]
    if isinstance(validators, list):
       validators = ", ".join(validators)
    return validators.strip()


@routes.route("/modules", methods=["GET"])
def get_all_modules():
    """
    Retourne tous les modules.

    .. :quickref: Permissions;

    :returns: un tableau de dictionnaire contenant les infos des modules.
    """
    q = DB.session.query(TModules)
    modules = []
    for module in q.all():
        module = format_keys_to_camel_case(module.as_dict())
        modules.append(module)
    return jsonify(modules), 200


@routes.route("/actions", methods=["GET"])
def get_all_actions():
    """
    Retourne toutes les actions.

    .. :quickref: Permissions;

    :returns: un tableau de dictionnaire contenant les infos des actions.
    """
    q = DB.session.query(TActions)
    actions = []
    for action in q.all():
        action = format_keys_to_camel_case(action.as_dict())
        actions.append(action)
    return jsonify(actions), 200


@routes.route("/filters", methods=["GET"])
def get_all_filters():
    """
    Retourne tous les types de filtres.

    .. :quickref: Permissions;

    :returns: un tableau de dictionnaire contenant les infos des filtres.
    """
    q = DB.session.query(BibFiltersType)
    filters = []
    for pfilter in q.all():
        pfilter = format_keys_to_camel_case(pfilter.as_dict())
        filters.append(pfilter)
    return jsonify(filters), 200


@routes.route("/objects", methods=["GET"])
def get_all_objects():
    """
    Retourne toutes les objets.

    .. :quickref: Permissions;

    :returns: un tableau de dictionnaire contenant les infos des objets.
    """
    q = DB.session.query(TObjects)
    objects = []
    for obj in q.all():
        obj = format_keys_to_camel_case(obj.as_dict())
        objects.append(obj)
    return jsonify(objects), 200


def format_keys_to_camel_case(d):
    if isinstance(d, list):
        output = []
        for item in d:
            output.append(format_keys_to_camel_case(item))
        return output
    elif isinstance(d, dict) :
        return dict((format_to_camel_case(k), v) for k, v in d.items())
    else:
        raise TypeError('formating to camel case accept only dict or list of dict')


def format_to_camel_case(snake_str):
    components = snake_str.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])


def get_requests():
    return [
        { 
            'token': '64750cf4-b14c-4793-bfd4-fcdc4defc7fe', 
            'user_name': 'Jean-Pascal MILCENT', 'organism_name': 'CBNA', 
            'end_access_date': '2021-01-12', 
            'permissions': {'sensitive': True, 'geographic': [27369, 27366, 9861], 'taxonomic': [187494]},
            'state': 'pending',
        },
        { 
            'token': 'fbd59621-cb02-4e44-8461-15b67b3ae76d', 
            'user_name': 'Martin DUPOND', 'organism_name': 'PNE', 
            'end_access_date': '2021-01-24', 
            'permissions': {'sensitive': False, 'geographic': [28512, 3974, 28528], 'taxonomic': [699191]},
            'state': 'pending',
        },
        { 
            'token': '6fc958d8-de23-4b2d-b162-fc334b64f61d', 
            'user_name': 'Zazi SWAROSKI', 'organism_name': 'CEN-PACA', 
            'end_access_date': '2021-02-02', 
            'permissions': {'sensitive': True, 'geographic': [34513, 10167, 7940], 'taxonomic': [185214]},
            'state': 'pending',
        },
        {
            'token': '477e51b4-17fd-42e6-85a0-0f3805a05c63', 
            'user_name': 'Robert BAYLE', 'organism_name': 'CBNA', 
            'end_access_date': '2021-03-22',
            'permissions': {'sensitive': False, 'geographic': [26574, 8312, 27440], 'taxonomic': [187415]},
            'state': 'pending',
        },
    ]


@routes.route("/requests", methods=["GET"])
def get_permissions_requests():
    """
    Retourne toutes les demandes de permissions avec des info sur 
    l'utilisateur ayant fait la demande.

    .. :quickref: Permissions;
    
    Params:
    :param state: filtre permetant de récupérer seulement les requêtes
    acceptées (accepted), refusées (refused), refusées et acceptées 
    (processed) ou en attentes (pending).
    :type state: 'accepted', 'refused', 'processed', 'pending'

    :returns: un tableau de dictionnaire contenant les infos des demandes
    de permissions.
    """
    params = request.args.to_dict()
    requests = get_requests()
    if "state" in params:
        for rqt in requests:
            if params["state"] == "processed":
                rqt["state"] = random.choice(["accepted", "refused"])
            elif params["state"] == "accepted":
                rqt["state"] = "accepted"
            elif params["state"] == "refused":
                rqt["state"] = "refused"

    requests = format_keys_to_camel_case(requests)
    return jsonify(requests), 200


@routes.route("/requests/<token>", methods=["GET"])
def get_permissions_requests_by_token(token):
    """
    Retourne le détail d'une demande.

    .. :quickref: Permissions;

    :returns: un dictionnaire avec les infos d'une demande de permission.
    """
    requests = get_requests()

    response = False
    for request in requests:
        if request['token'] == token:
            response = request
            break

    if not response:
        response = {
            "message": f"Token de demande introuvable : {token} .",
            "status": "error"
        }
        return response, 404
    else:
        response = format_keys_to_camel_case(response)
        return response, 200


def get_roles_permissions():
    return [
        { 'id': 1, 'name': 'Jean-Pascal MILCENT', 'type': 'USER', 'permissionsNbr': 5, 'permissions': [
            {'module': 'SYNTHESE', 'action': 'R', 'object': 'PRIVATE_OBSERVATION', 'filter_type': 'PRECISION', 'filter_value': 'precise'},
            {'module': 'SYNTHESE', 'action': 'R', 'object': 'PRIVATE_OBSERVATION', 'filter_type': 'GEOGRAPHIC', 'filter_value': '3896,18628'},
            {'module': 'SYNTHESE', 'action': 'E', 'object': 'PRIVATE_OBSERVATION', 'filter_type': 'PRECISION', 'filter_value': 'precise'},
            {'module': 'SYNTHESE', 'action': 'E', 'object': 'PRIVATE_OBSERVATION', 'filter_type': 'GEOGRAPHIC', 'filter_value': '3896,18628'},
        ]},
        { 'id': 2, 'name': 'Martin DUPOND', 'type': 'USER', 'permissionsNbr': 3 },
        { 'id': 3, 'name': 'Observateurs', 'type': 'GROUP', 'permissionsNbr': 6 },
        { 'id': 4, 'name': 'Zazi SWAROSKI', 'type': 'USER', 'permissionsNbr': 15 },
        { 'id': 5, 'name': 'Utilisateurs de GeoNature', 'type': 'GROUP', 'permissionsNbr': 8 },
        { 'id': 6, 'name': 'Administrateurs de GeoNature', 'type': 'GROUP', 'permissionsNbr': 25 },
        { 'id': 7, 'name': 'Raphaël LEPEINTRE', 'type': 'USER', 'permissionsNbr': 5 },
        { 'id': 8, 'name': 'Robert BAYLE', 'type': 'USER', 'permissionsNbr': 3 },
        { 'id': 9, 'name': 'Jean-Baptiste GIBELIN', 'type': 'USER', 'permissionsNbr': 6 },
        { 'id': 10, 'name': 'Louise NADAL', 'type': 'USER', 'permissionsNbr': 15 },
        { 'id': 11, 'name': 'Anne POLZE', 'type': 'USER', 'permissionsNbr': 8 },
        { 'id': 12, 'name': 'Scipion BAYLE', 'type': 'USER', 'permissionsNbr': 25 },
        { 'id': 13, 'name': 'Hélène TOURRE', 'type': 'USER', 'permissionsNbr': 5 },
        { 'id': 14, 'name': 'Étienne POMMEL', 'type': 'USER', 'permissionsNbr': 3 },
        { 'id': 15, 'name': 'Jeanne DOMERGUE', 'type': 'USER', 'permissionsNbr': 6 },
        { 'id': 16, 'name': 'Jacques DALVERNY', 'type': 'USER', 'permissionsNbr': 15 },
        { 'id': 17, 'name': 'Marie FABRE', 'type': 'USER', 'permissionsNbr': 8 },
        { 'id': 18, 'name': 'Pierre FONTANIEU', 'type': 'USER', 'permissionsNbr': 25 },
    ]


@routes.route("/roles", methods=["GET"])
def get_permissions_for_all_roles():
    """
    Retourne tous les rôles avec leur nombre de permissions.

    .. :quickref: Permissions;

    :returns: un tableau de dictionnaire contenant les infos du rôle et son nombre de permissions.
    """
    roles_permissions = get_roles_permissions()
    return jsonify(roles_permissions), 200


@routes.route("/roles/<int:id_role>", methods=["GET"])
def get_permissions_by_role_id(id_role):
    """
    Retourne un rôle avec son nombre de permissions.

    .. :quickref: Permissions;

    :returns: un dictionnaire avec les infos du rôle et son nombre de permissions.
    """
    roles_permissions = get_roles_permissions()

    response = False
    for role in roles_permissions:
        if role['id'] == id_role:
            response = role
            break

    if not response:
        response = {
            "message": f"Id de rôle introuvable : {id_role} .",
            "status": "error"
        }
        return response, 404
    else:
        return response, 200
